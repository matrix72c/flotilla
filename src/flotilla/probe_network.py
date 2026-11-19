"""Content-bearing TCP/UDP controls; a failed control never proves isolation.

Every check here allows the traffic first, then denies it, then allows it again. A single blocked
exchange proves nothing -- a listener that was never up, a name that did not resolve and a policy
that is working all look identical from the client side. Only the allow/deny/allow sequence tells
them apart, and anything that arrives during the deny step is a leak regardless of what the other
samples did.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from flotilla.capabilities import Evidence, ExternalForm, ExternalTarget
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


async def inbound_isolation(run: _Run) -> Evidence:
    """A sandbox must not be reachable from one that was not given access to it.

    Two members of one group: the source's egress is allowed independently, so a blocked exchange
    is the target's ingress rule and not the source's egress.
    """
    p = run.platform
    members = {}
    for index in range(2):
        handle = await run._create(run._spec(f"inbound-{index}", group=run.run_id))
        members[str(index)] = handle
        await p.start_process(handle, python(_SERVER, background=True))
    topology = Topology({"probe": frozenset(members)})
    await p.link(members, topology)
    addresses = {name: await p.internal_address(handle) for name, handle in members.items()}
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
                return Evidence(False, "listener positive control failed; inbound isolation not established")
    outsider, target = members["0"], members["1"]
    observations: list[dict[str, Any]] = []
    for protocol in ("tcp", "udp"):
        observation: dict[str, Any] = {"protocol": protocol}
        observations.append(observation)
        await policy(p, outsider, egress=(target,), ingress=(target,))
        await policy(p, target, egress=(outsider,), ingress=(outsider,))
        observation["allowed"] = await _link_samples(run, outsider, addresses["1"], protocol, run.run_id + "allow")
        if _positive_outcome(observation["allowed"]) != "received":
            continue
        await policy(p, target, egress=(outsider,), ingress=())
        observation["denied"] = await _link_samples(run, outsider, addresses["1"], protocol, run.run_id + "deny")
        # Re-enable to distinguish isolation from a listener that simply died.
        await policy(p, target, egress=(outsider,), ingress=(outsider,))
        observation["restored"] = await _link_samples(run, outsider, addresses["1"], protocol, run.run_id + "restore")
    leaked = [
        o["protocol"] for o in observations if _deny_outcome(o.get("denied", [])) in ("received", "wrong-content")
    ]
    if leaked:
        return Evidence(False, f"a sandbox denied ingress stayed reachable over {leaked}")
    complete = len(observations) == 2 and all(
        _positive_outcome(o.get("allowed", [])) == "received"
        and _deny_outcome(o.get("denied", ["error"])) == "blocked"
        and _positive_outcome(o.get("restored", [])) == "received"
        for o in observations
    )
    return Evidence(
        complete,
        "TCP and UDP ingress allow/deny/restore held" if complete else f"ingress controls incomplete: {observations}",
    )


async def egress(run: _Run, *, mode: str = "allowlist") -> tuple[Evidence, bool]:
    """Whether a deny policy is actually enforced, and whether the allowing mode worked at all.

    Returns the deny evidence and whether `mode` was exercised end to end, which is an observation
    about the probe's own configuration rather than an isolation property.
    """
    targets = run.settings.targets
    if not targets:
        return Evidence(False, "probe.external_targets not configured, so egress was not measured"), False
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
        observation = {"host": target.host, "port": target.port, "protocol": target.protocol}
        observations.append(observation)
        # Legacy echo targets retain their original behavior. Standard services pin resolution
        # once under the allowing policy, then use that exact address for all three exchanges.
        try:
            ip = target.host if target.service == "echo" else await resolve_target(run, handle, target)
        except ValueError:
            observation["allowed"] = "resolution-error"
            break
        observation["address"] = ip
        if target.service != "echo" and mode != "any":
            external = ExternalPolicy(mode="allowlist", cidrs=(ip,))
            await policy(run.platform, handle, external=external)
        observation["allowed"] = await service_exchange(run, handle, target, ip, run.run_id + "external")
        if observation["allowed"] != "received":
            break
        await policy(run.platform, handle, external=ExternalPolicy(mode="none"))
        observation["denied"] = await service_exchange(run, handle, target, ip, run.run_id + "denied")
        await policy(run.platform, handle, external=external)
        observation["restored"] = await service_exchange(run, handle, target, ip, run.run_id + "restored")
        await run._delete(handle.iid)
        if observation["denied"] == "error" or observation["restored"] != "received":
            break
    leaked = [o["protocol"] for o in observations if o.get("denied") in ("received", "wrong-content")]
    complete = len(observations) == len(targets) and all(
        o.get("allowed") == o.get("restored") == "received" and o.get("denied") != "error" for o in observations
    )
    if leaked:
        return Evidence(False, f"an external target stayed reachable under a deny policy over {leaked}"), complete
    if not complete:
        return Evidence(False, f"external allow/deny/restore incomplete: {observations}"), False
    covered = {target.protocol for target in targets}
    if covered != {"tcp", "udp"}:
        return Evidence(
            False, f"egress measured over {sorted(covered)} only; both TCP and UDP controls are required"
        ), complete
    return Evidence(True, f"TCP and UDP deny enforced after a working {mode} policy"), complete


async def resolvers_of(run: _Run, handle: InstanceHandle) -> tuple[Evidence, tuple[str, ...], tuple[str, ...]]:
    """Which resolvers the platform gives a sandbox, and which of them answer under a deny policy.

    Cluster DNS reaching out under "no external access" is normal and expected; it is recorded as
    implicit egress rather than treated as a leak, because a task asking for no network has to know.
    """
    if not run.settings.dns_name:
        return Evidence(False, "probe.dns_name not configured, so resolver behavior was not measured"), (), ()
    resolver_file = (await run.platform.read_file(handle, "/etc/resolv.conf")).decode()
    resolvers = tuple(line.split()[1] for line in resolver_file.splitlines() if line.startswith("nameserver "))
    if not resolvers:
        return Evidence(False, "no resolver addresses found in /etc/resolv.conf"), (), ()
    observations: list[str] = []
    for host in resolvers:
        external = ExternalPolicy(mode="allowlist", cidrs=(host,))
        for protocol in ("tcp", "udp"):
            await policy(run.platform, handle, external=external)
            control = await run.platform.exec(handle, python(_DNS, host, protocol, run.settings.dns_name))
            if control.exit_code != 0 or control.stdout.strip() != b"received":
                return (
                    Evidence(False, f"DNS positive control failed for {host}/{protocol}"),
                    resolvers,
                    tuple(observations),
                )
            await policy(run.platform, handle, external=ExternalPolicy(mode="none"))
            result = await run.platform.exec(handle, python(_DNS, host, protocol, run.settings.dns_name))
            if result.exit_code != 0 or result.stdout.strip() not in (b"received", b"blocked"):
                return (
                    Evidence(False, f"DNS deny check incomplete for {host}/{protocol}"),
                    resolvers,
                    tuple(observations),
                )
            await policy(run.platform, handle, external=external)
            restored = await run.platform.exec(handle, python(_DNS, host, protocol, run.settings.dns_name))
            await policy(run.platform, handle, external=ExternalPolicy(mode="none"))
            if restored.exit_code != 0 or restored.stdout.strip() != b"received":
                return (
                    Evidence(False, f"DNS restore control failed for {host}/{protocol}"),
                    resolvers,
                    tuple(observations),
                )
            if result.stdout.strip() == b"received":
                target = f"[{host}]" if ":" in host else host
                observations.append(f"{target}:53/{protocol}")
    return (
        Evidence(True, "resolver TCP/UDP behavior measured; not an exhaustive implicit-egress claim"),
        resolvers,
        tuple(observations),
    )


@dataclass(frozen=True)
class NetworkIsolation:
    """What the network checks established, before it becomes part of `IsolationEvidence`."""

    external_none: Evidence
    network_acceptance: Evidence
    inbound_isolation: Evidence
    external_forms: frozenset[ExternalForm] = frozenset()
    implicit_egress: tuple[str, ...] = ()
    cluster_dns: tuple[str, ...] = ()


async def measure_isolation(run: _Run, handle: InstanceHandle) -> NetworkIsolation:
    """Egress enforcement, resolver behavior and inbound isolation, in that order."""
    from flotilla.probe_checks import attempt

    inbound = await attempt(lambda: inbound_isolation(run))
    # Release the group before the egress checks create their own instances.
    for iid in list(run._created):
        if iid != handle.iid:
            await run._delete(iid)
    result = await attempt(lambda: egress(run))
    none, allowlist_worked = (result, False) if isinstance(result, Evidence) else result
    forms: set[ExternalForm] = {"allowlist"} if allowlist_worked else set()
    if none.held:
        forms.add("none")
    if run.settings.test_external_any:
        any_result = await attempt(lambda: egress(run, mode="any"))
        if not isinstance(any_result, Evidence):
            any_none, any_worked = any_result
            if any_worked:
                forms.add("any")
            # A leak observed later cannot be hidden by the earlier allowlist controls.
            if not any_none.held and "stayed reachable" in any_none.observed:
                none = any_none
    dns_evidence = await attempt(lambda: resolvers_of(run, handle))
    resolvers: tuple[str, ...] = ()
    implicit: tuple[str, ...] = ()
    if isinstance(dns_evidence, Evidence):
        dns_item = dns_evidence
    else:
        dns_item, resolvers, implicit = dns_evidence
    accepted = none.held and dns_item.held
    return NetworkIsolation(
        external_none=none,
        network_acceptance=Evidence(
            accepted,
            "the platform enforced the policy it was sent, with measured cluster DNS behavior; "
            "not complete network disconnection"
            if accepted
            else f"policy enforcement or resolver behavior unproven: {none.observed}; {dns_item.observed}",
        ),
        inbound_isolation=inbound,
        external_forms=frozenset(forms),
        implicit_egress=implicit,
        cluster_dns=resolvers,
    )


__all__ = ["NetworkIsolation", "egress", "inbound_isolation", "measure_isolation"]
