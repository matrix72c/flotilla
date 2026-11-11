"""Content-bearing TCP/UDP controls; a failed control never proves isolation."""

from __future__ import annotations

import ipaddress
from collections import Counter
from typing import Any

from flotilla.capabilities import ExternalTarget, Item, Network
from flotilla.platform.base import ExternalPolicy, InstanceHandle, ProcessSpec, Topology
from flotilla.probe import _Run
from flotilla.probe_opensandbox import policy

# Executed only in a probe sandbox. No remote package installation or protocol fallback.
_SERVER = """import socket,threading,time

def serve(kind):
 s=socket.socket(socket.AF_INET,kind); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('0.0.0.0',48080))
 if kind==socket.SOCK_STREAM: s.listen(64)
 while True:
  if kind==socket.SOCK_DGRAM:
   data,peer=s.recvfrom(4096); s.sendto(data,peer)
  else:
   conn,_=s.accept()
   with conn:
    conn.settimeout(3); data=conn.recv(4096); conn.sendall(data)
for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM): threading.Thread(target=serve,args=(kind,),daemon=True).start()
time.sleep(3600)
"""
_CLIENT = """import socket,sys
host,port,proto,token=sys.argv[1:]; kind=socket.SOCK_STREAM if proto=='tcp' else socket.SOCK_DGRAM
try:
 family,_,_,_,address=socket.getaddrinfo(host,int(port),type=kind)[0]
 s=socket.socket(family,kind); s.settimeout(3); s.connect(address); s.sendall(token.encode()); data=s.recv(4096)
 print('received' if data==token.encode() else 'wrong-content')
except (TimeoutError,ConnectionRefusedError,ConnectionResetError): print('blocked')
except OSError: print('error')
"""
_RESOLVE = """import socket,sys
host,port,proto=sys.argv[1:]
kind=socket.SOCK_STREAM if proto=='tcp' else socket.SOCK_DGRAM
print(socket.getaddrinfo(host,int(port),type=kind)[0][4][0])
"""
_STANDARD_CLIENT = r"""import os,re,socket,ssl,struct,sys,time
ip,port,service,host=sys.argv[1:]; port=int(port); connected=False
kind=socket.SOCK_STREAM if service=='https' else socket.SOCK_DGRAM
deadline=time.monotonic()+3

def budget(s):
 remaining=deadline-time.monotonic()
 if remaining<=0: raise TimeoutError('exchange deadline')
 s.settimeout(remaining)

try:
 with socket.socket(socket.AF_INET6 if ':' in ip else socket.AF_INET,kind) as raw:
  budget(raw); raw.connect((ip,port)); connected=True
  if service=='https':
   # Never disable CA/hostname verification; the pinned IP is only the connection address.
   context=ssl.create_default_context()
   budget(raw)
   with context.wrap_socket(raw,server_hostname=host) as s:
    authority=('['+host+']') if ':' in host else host
    if port!=443: authority+=':'+str(port)
    request=('HEAD / HTTP/1.1\r\nHost: '+authority+'\r\nConnection: close\r\n\r\n').encode('ascii')
    budget(s); s.sendall(request); data=b''
    while b'\r\n\r\n' not in data:
     if len(data)>=16384: raise ValueError('oversized HTTP headers')
     budget(s); part=s.recv(min(4096,16384-len(data)))
     if not part: raise ValueError('incomplete HTTP headers')
     data+=part
    lines=data.split(b'\r\n\r\n',1)[0].split(b'\r\n')
    if not re.fullmatch(rb'HTTP/1\.[01] [2-5][0-9]{2}(?: [\x20-\x7e\x80-\xff]*)?',lines[0]):
     raise ValueError('invalid HTTP status')
    for line in lines[1:]:
     if not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[\t\x20-\x7e\x80-\xff]*",line):
      raise ValueError('invalid HTTP header')
  else:
   # Real NTP seconds plus an unpredictable fractional timestamp, echoed as originate.
   token=struct.pack('!I',(int(time.time())+2208988800)&0xffffffff)+os.urandom(4)
   request=bytes([0x23])+bytes(39)+token
   budget(raw); raw.sendall(request); budget(raw); data=raw.recv(512)
   if not (len(data)>=48 and data[0]&7==4 and (data[0]>>3)&7 in (3,4)
           and data[0]>>6!=3 and 1<=data[1]<=15
           and data[24:32]==token and data[40:48]!=bytes(8)):
    raise ValueError('invalid NTP response')
 print('received')
except (TimeoutError,ConnectionRefusedError,ConnectionResetError):
 # A connected HTTPS peer stalling/resetting is not evidence that TCP was blocked.
 print('wrong-content' if service=='https' and connected else 'blocked')
except ssl.SSLError: print('error')
except ValueError: print('wrong-content')
except OSError: print('error')
"""
_DNS = r"""import socket,struct,sys
host,proto,name=sys.argv[1:]; ident=21371
q=struct.pack('!HHHHHH',ident,256,1,0,0,0)
q+=b''.join(bytes([len(p)])+p.encode() for p in name.split('.'))+b'\x00\x00\x01\x00\x01'
kind=socket.SOCK_STREAM if proto=='tcp' else socket.SOCK_DGRAM
s=socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET,kind); s.settimeout(3)
try:
 s.connect((host,53))
 if proto=='tcp':
  s.sendall(struct.pack('!H',len(q))+q); header=s.recv(2)
  if len(header)!=2: raise ValueError('short DNS length')
  size=struct.unpack('!H',header)[0]; data=b''
  while len(data)<size:
   part=s.recv(size-len(data))
   if not part: raise ValueError('short DNS response')
   data+=part
 else: s.sendall(q); data=s.recv(4096)
 print('received' if len(data)>=12 and struct.unpack('!H',data[:2])[0]==ident and data[2]&128 else 'error')
except (TimeoutError,ConnectionRefusedError,ConnectionResetError): print('blocked')
"""


def python(script: str, *args: str | int, background: bool = False) -> ProcessSpec:
    return ProcessSpec(
        argv=("python3", "-I", "-c", script, *map(str, args)),
        uid=0,
        gid=0,
        cwd="/",
        env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        timeout_s=None if background else 15,
    )


async def exchange(run: _Run, source: InstanceHandle, host: str, port: int, protocol: str, token: str) -> str:
    result = await run.platform.exec(source, python(_CLIENT, host, port, protocol, token))
    if result.exit_code != 0 or result.timed_out:
        return "error"
    value = result.stdout.decode().strip()
    return value if value in ("received", "blocked", "wrong-content") else "error"


async def resolve_target(run: _Run, handle: InstanceHandle, target: ExternalTarget) -> str:
    """Select one address while allowed; DNS is never part of a subsequent deny check."""
    try:
        return str(ipaddress.ip_address(target.host))
    except ValueError:
        pass
    result = await run.platform.exec(handle, python(_RESOLVE, target.host, target.port, target.protocol))
    if result.exit_code != 0 or result.timed_out:
        raise ValueError("external target resolution failed")
    return str(ipaddress.ip_address(result.stdout.decode().strip()))


async def service_exchange(run: _Run, handle: InstanceHandle, target: ExternalTarget, ip: str, token: str) -> str:
    if target.service == "echo":
        return await exchange(run, handle, ip, target.port, target.protocol, token)
    result = await run.platform.exec(handle, python(_STANDARD_CLIENT, ip, target.port, target.service, target.host))
    if result.exit_code != 0 or result.timed_out:
        return "error"
    value = result.stdout.decode().strip()
    return value if value in ("received", "blocked", "wrong-content") else "error"


def verdict(ok: bool, text: str, **measurements: Any) -> Item:
    return Item(status="passed" if ok else "failed", evidence=text, measurements=measurements)


async def _link_samples(run: _Run, source: InstanceHandle, host: str, protocol: str, token: str) -> list[str]:
    """Fixed UDP sampling, not retry-until-success. Retain misses and unexpected responses."""
    return [
        await exchange(run, source, host, 48080, protocol, f"{index}/{token}")
        for index in range(3 if protocol == "udp" else 1)
    ]


def _positive_outcome(samples: list[str]) -> str:
    # A later valid response must not hide a command error or a mismatched payload.
    for outcome in ("wrong-content", "error", "received"):
        if outcome in samples:
            return outcome
    return "blocked"


def _deny_outcome(samples: list[str]) -> str:
    # Any received data under deny is a leak, even when other samples time out or error.
    for outcome in ("received", "wrong-content", "error"):
        if outcome in samples:
            return outcome
    return "blocked"


async def links(run: _Run) -> tuple[Item, Item, Item]:
    p = run.platform
    members = {}
    for index in range(run.settings.link_members):
        handle = await run._create(run._spec(f"link-{index}", group=run.run_id))
        members[str(index)] = handle
        await p.start_process(handle, python(_SERVER, background=True))
    topology = Topology({"probe": frozenset(members)})
    await p.link(members, topology)
    addresses = {name: await p.internal_address(handle) for name, handle in members.items()}
    checks = {}
    # Readiness is a real loopback content exchange, not merely a Running state.
    for handle in members.values():
        for protocol in ("tcp", "udp"):
            ready = "error"
            for _ in range(10):
                ready = await exchange(run, handle, "127.0.0.1", 48080, protocol, run.run_id)
                if ready == "received":
                    break
                await run.clock.sleep(0.5)
            if ready != "received":
                error = Item(status="error", evidence="listener positive control failed")
                return error, error, error
    for protocol in ("tcp", "udp"):
        pairs = []
        failed_pairs = []
        observations = []
        for name, handle in members.items():
            for target_name in members:
                if name != target_name:
                    samples = await _link_samples(
                        run, handle, addresses[target_name], protocol, f"{run.run_id}:{name}:{target_name}:{protocol}"
                    )
                    observations.append({"source": name, "target": target_name, "outcomes": samples})
                    value = _positive_outcome(samples)
                    pairs.append(value)
                    if value != "received":
                        failed_pairs.append({"source": name, "target": target_name, "outcome": value})
        checks[protocol] = verdict(
            all(value == "received" for value in pairs),
            "directed member-pair reachability (1 TCP / 3 UDP samples); not a lossless-delivery guarantee; "
            "unreachable alone does not distinguish policy from routing or loss",
            members=len(members),
            addresses=addresses,
            pairs=len(pairs),
            outcomes=dict(Counter(pairs)),
            # Bounded by the configured maximum of 32 members; never retain payloads or credentials.
            samples=observations,
            failed_pairs=failed_pairs,
        )
        if "error" in pairs:
            checks[protocol] = checks[protocol].model_copy(
                update={"status": "error", "evidence": "peer exchange check incomplete"}
            )
    # Inbound isolation has its own positive controls; unrelated matrix failures do not skip it.
    outsider = members["0"]
    target = members["1"]
    inbound: list[dict[str, Any]] = []
    unsupported = False
    try:
        for protocol in ("tcp", "udp"):
            observation: dict[str, Any] = {"protocol": protocol}
            inbound.append(observation)
            await policy(p, outsider, egress=(target,), ingress=(target,))
            await policy(p, target, egress=(outsider,), ingress=(outsider,))
            positive = await _link_samples(run, outsider, addresses["1"], protocol, run.run_id + "allow")
            observation["positive"] = positive
            if _positive_outcome(positive) != "received":
                continue
            await policy(p, target, egress=(outsider,), ingress=())
            negative = await _link_samples(run, outsider, addresses["1"], protocol, run.run_id + "deny")
            observation["denied"] = negative
            # Re-enable to distinguish isolation from listener failure.
            await policy(p, target, egress=(outsider,), ingress=(outsider,))
            observation["restored"] = await _link_samples(
                run, outsider, addresses["1"], protocol, run.run_id + "restore"
            )
    except NotImplementedError:
        unsupported = True
    leak = any(_deny_outcome(item.get("denied", [])) in ("received", "wrong-content") for item in inbound)
    complete = len(inbound) == 2 and all(
        _positive_outcome(item.get("positive", [])) == "received"
        and _deny_outcome(item.get("denied", ["error"])) == "blocked"
        and _positive_outcome(item.get("restored", [])) == "received"
        for item in inbound
    )
    inbound_item = Item(
        status="failed" if leak else "skipped" if unsupported else "passed" if complete else "error",
        evidence="TCP/UDP inbound allow/deny/restore with independently allowed source egress; "
        "all deny samples must be blocked",
        measurements={"controls": inbound},
    )
    # Real runtime link update removes a member; both protocols must become blocked and then recover.
    reduced = Topology({"probe": frozenset(set(members) - {"0"})})
    await p.link(members, reduced)
    for protocol in ("tcp", "udp"):
        denied = await _link_samples(run, outsider, addresses["1"], protocol, run.run_id + "unlinked")
        await p.link(members, topology)
        restored = await _link_samples(run, outsider, addresses["1"], protocol, run.run_id + "linked")
        checks[protocol] = checks[protocol].model_copy(
            update={
                "measurements": {
                    **checks[protocol].measurements,
                    "disconnect": {"denied": denied, "restored": restored},
                }
            }
        )
        if _deny_outcome(denied) in ("received", "wrong-content"):
            checks[protocol] = checks[protocol].model_copy(
                update={"status": "failed", "evidence": "disconnected member remained reachable"}
            )
        elif (_deny_outcome(denied) == "error" or _positive_outcome(restored) != "received") and checks[
            protocol
        ].status != "failed":
            checks[protocol] = checks[protocol].model_copy(
                update={"status": "error", "evidence": "disconnect/reconnect control incomplete"}
            )
        await p.link(members, reduced)
    return checks["tcp"], checks["udp"], inbound_item


async def egress(run: _Run, *, mode: str = "allowlist") -> tuple[Item, Item]:
    targets = run.settings.targets
    if not targets:
        skipped = Item(status="skipped", evidence="probe.external_targets not configured")
        return skipped, skipped
    observations: list[dict[str, Any]] = []
    for target in targets:
        try:
            ipaddress.ip_address(target.host)
            external = ExternalPolicy(mode="allowlist", cidrs=(target.host,))
        except ValueError:
            external = ExternalPolicy(mode="allowlist", hosts=(target.host,))
        if mode == "any":
            external = ExternalPolicy(mode="any")
        handle = await run._create(run._spec(f"external-{target.protocol}", external=external))
        observation = {
            "host": target.host,
            "port": target.port,
            "protocol": target.protocol,
            "service": target.service,
        }
        observations.append(observation)
        # Legacy echo targets retain their original behavior. Standard services pin resolution
        # once under the allowing policy, then use that exact address for all three exchanges.
        try:
            ip = target.host if target.service == "echo" else await resolve_target(run, handle, target)
        except ValueError:
            observation["positive"] = "resolution-error"
            break
        observation["address"] = ip
        if target.service != "echo" and mode != "any":
            external = ExternalPolicy(mode="allowlist", cidrs=(ip,))
            await policy(run.platform, handle, external=external)
        positive = await service_exchange(run, handle, target, ip, run.run_id + "external")
        observation["positive"] = positive
        if positive != "received":
            break
        await policy(run.platform, handle, external=ExternalPolicy(mode="none"))
        denied = await service_exchange(run, handle, target, ip, run.run_id + "denied")
        observation["denied"] = denied
        await policy(run.platform, handle, external=external)
        restored = await service_exchange(run, handle, target, ip, run.run_id + "restored")
        observation["restored"] = restored
        await run._delete(handle.iid)
        if denied == "error" or restored != "received":
            break
    measurements = {"targets": observations}
    leak = any(item.get("denied") in ("received", "wrong-content") for item in observations)
    complete = len(observations) == len(targets) and all(
        item.get("positive") == item.get("restored") == "received" and item.get("denied") != "error"
        for item in observations
    )
    allow = Item(
        status="passed" if complete else "error",
        evidence=f"configured {mode} service controls" if complete else "external positive/restore control incomplete",
        measurements=measurements,
    )
    coverage = {target.protocol for target in targets}
    if leak:
        none = Item(
            status="failed", evidence="external target remained reachable under deny", measurements=measurements
        )
    elif not complete:
        none = Item(status="error", evidence="external policy control incomplete", measurements=measurements)
    elif coverage != {"tcp", "udp"}:
        none = Item(
            status="skipped", evidence="none requires configured TCP and UDP controls", measurements=measurements
        )
    else:
        none = Item(
            status="passed",
            evidence=f"TCP/UDP same-target deny after positive {mode} controls",
            measurements=measurements,
        )
    return none, allow


async def dns(run: _Run, handle: InstanceHandle) -> tuple[Item, tuple[str, ...], tuple[str, ...]]:
    if not run.settings.dns_name:
        return Item(status="skipped", evidence="probe.dns_name not configured"), (), ()
    resolver_file = (await run.platform.read_file(handle, "/etc/resolv.conf")).decode()
    resolvers = tuple(line.split()[1] for line in resolver_file.splitlines() if line.startswith("nameserver "))
    if not resolvers:
        return Item(status="error", evidence="no resolver addresses found"), (), ()
    observations: list[str] = []
    for host in resolvers:
        external = ExternalPolicy(mode="allowlist", cidrs=(host,))
        for protocol in ("tcp", "udp"):
            await policy(run.platform, handle, external=external)
            control = await run.platform.exec(handle, python(_DNS, host, protocol, run.settings.dns_name))
            if control.exit_code != 0 or control.stdout.strip() != b"received":
                return Item(status="error", evidence="DNS positive control failed"), resolvers, tuple(observations)
            await policy(run.platform, handle, external=ExternalPolicy(mode="none"))
            result = await run.platform.exec(handle, python(_DNS, host, protocol, run.settings.dns_name))
            if result.exit_code != 0 or result.stdout.strip() not in (b"received", b"blocked"):
                return Item(status="error", evidence="DNS negative check incomplete"), resolvers, tuple(observations)
            await policy(run.platform, handle, external=external)
            restored = await run.platform.exec(handle, python(_DNS, host, protocol, run.settings.dns_name))
            await policy(run.platform, handle, external=ExternalPolicy(mode="none"))
            if restored.exit_code != 0 or restored.stdout.strip() != b"received":
                return (
                    Item(status="error", evidence="DNS restored positive control failed"),
                    resolvers,
                    tuple(observations),
                )
            if result.stdout.strip() == b"received":
                target = f"[{host}]" if ":" in host else host
                observations.append(f"{target}:53/{protocol}")
    return (
        Item(status="passed", evidence="TCP/UDP resolver behavior measured; not an exhaustive implicit-egress claim"),
        resolvers,
        tuple(observations),
    )


async def measure_network(run: _Run, handle: InstanceHandle) -> Network:
    from flotilla.probe_checks import attempt

    link_result = await attempt(lambda: links(run))
    if isinstance(link_result, Item):
        tcp = udp = inbound = link_result
    else:
        tcp, udp, inbound = link_result
    # Release the larger group before ordinary/privileged phases.
    for iid in list(run._created):
        if iid != handle.iid:
            await run._delete(iid)
    external_result = await attempt(lambda: egress(run))
    none, allow = (external_result, external_result) if isinstance(external_result, Item) else external_result
    any_item = Item(status="skipped", evidence="unrestricted external mode is not selected")
    if run.settings.test_external_any:
        any_result = await attempt(lambda: egress(run, mode="any"))
        any_item = any_result if isinstance(any_result, Item) else any_result[1]
        if not isinstance(any_result, Item) and any_result[0].status == "failed":
            # A later observed deny leak cannot be hidden by earlier allowlist controls.
            none = any_result[0]
    dns_result = await attempt(lambda: dns(run, handle))
    dns_item, resolvers, observations = (dns_result, (), ()) if isinstance(dns_result, Item) else dns_result
    # Resolver non-53 controls are optional: users need not operate services on shared cluster DNS.
    # Keep their evidence separate; neither DNS success nor absent controls proves IP-wide isolation.
    non_dns_controls = {(target.host, target.protocol) for target in run.settings.targets if target.port != 53}
    resolver_controls = bool(resolvers) and all(
        (host, protocol) in non_dns_controls for host in resolvers for protocol in ("tcp", "udp")
    )
    resolver_non_dns = Item(
        status="skipped", evidence="resolver non-DNS TCP/UDP controls not configured for every resolver"
    )
    if resolver_controls:
        resolver_non_dns = Item(
            status="passed" if none.ok else "error",
            evidence=(
                "configured resolver non-DNS controls passed within external allow/deny/restore checks"
                if none.ok
                else "external controls incomplete or failed; resolver non-DNS isolation not established"
            ),
        )
    accepted = none.ok and dns_item.ok
    acceptance_evidence = (
        "bounded external TCP/UDP isolation with measured cluster DNS behavior; not complete network disconnection"
        if accepted
        else "external TCP/UDP isolation or DNS behavior checks incomplete or failed"
    )
    return Network(
        link=tcp,
        link_udp=udp,
        inbound_isolation=inbound,
        link_max_members=run.settings.link_members,
        external_none=none,
        external_allowlist=allow,
        external_any=any_item,
        dns=dns_item,
        resolver_non_dns=resolver_non_dns,
        internal_address="exec",
        cluster_dns=resolvers,
        implicit_egress=observations,
        network_acceptance=Item(status="passed" if accepted else "error", evidence=acceptance_evidence),
    )
