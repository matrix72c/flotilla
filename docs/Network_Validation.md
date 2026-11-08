# Network validation

Use a dedicated deployment and isolated probe labels. The only handwritten inputs are in `deployment.toml`; invoke `flotilla probe --deployment deployment.toml --out capabilities.candidate.json`. The report records effective inputs and the relevant configuration fingerprint without tokens or complete private responses.

## Positive controls are mandatory

A timeout, missing tool, DNS error, dead listener or wrong echo is not evidence of isolation. Probe uses unique content, not just successful TCP connection establishment. It first verifies the listener or target under an allowing policy, then applies a denying policy, and finally restores the positive control. TCP and UDP are independent checks.

`[probe].external_targets` selects a service for each explicit `host`, `port` and `protocol`. Supply both TCP and UDP controls; you need not operate raw echo servers. Supported services are:

- `service = "echo"` (default): TCP or UDP endpoint that returns the exact probe token. Existing configurations and direct `ProbeSettings` three-tuple targets retain this behavior.
- `service = "https"`: TCP HTTPS endpoint. Probe sends `HEAD /`, requires a valid final HTTP/1.0 or HTTP/1.1 status and complete headers (at most 16 KiB), and verifies the certificate against the original hostname using the image's CA trust store. A valid error status also proves reachability. Certificate errors, malformed/oversized responses, and stalls after TCP connection cannot prove isolation. No redirects are followed and no arbitrary echo payload is sent to HTTPS.
- `service = "ntp"`: UDP NTP endpoint. A version-4 client request carries current NTP seconds and a random fractional timestamp. The response must have server mode, version 3/4, synchronized leap status, stratum 1–15, an exactly matching originate timestamp and a nonzero transmit timestamp. Kiss-of-death, unrelated and malformed responses cannot establish a positive control.

For HTTPS and NTP, probe resolves the hostname once under the allowed policy, selects one address, and pins that same IP for all three phases. The allowlist is narrowed to that IP for the exchanges; HTTPS retains the original TLS SNI/verification name and HTTP Host. There is no address fallback or phase retry. Results record the service, selected IP and phase outcomes, not response contents. A failed positive or restore control prevents acceptance; any observed deny response fails isolation, even if another protocol is missing.

For example, if these public services are approved and reachable from your deployment:

```toml
[probe]
# Keep your other probe inputs, including the pinned image.
dns_name = "pypi.org"
external_targets = [
  { host = "pypi.org", port = 443, protocol = "tcp", service = "https" },
  { host = "time.cloudflare.com", port = 123, protocol = "udp", service = "ntp" },
]
```

These are explicit examples, not built-in defaults or a guarantee of availability. Probe never guesses targets from `[network].external` or a hard-coded public address. Test targets apply only to probe instances; they do not expand workload policy. The probe image needs Python's standard-library TLS support and a valid CA trust store; probe never installs packages remotely.

## Peer links and inbound isolation

The configured `link_members` defaults to nine. Probe creates that many members, verifies every directed pair on TCP and UDP, disconnects a member through the real topology API, and checks that reconnecting restores content exchange. The report means “this group size was tested”, not “this is the platform maximum”.

Peer checks take one TCP sample and a fixed three UDP samples, with distinct tokens, in both allowing and denying phases. They always complete the sample set, not retry until success. An allowed UDP pair needs at least one matching response and no command errors or mismatched responses; this establishes bounded reachability, **not lossless delivery**. Every sample, including timeouts, and the member addresses remain in the report. A timeout alone does not identify policy, routing or packet loss as the cause. Deny requires every sample to be blocked; any received data is a failure and cannot be hidden by later timeouts or failed restoration. Inbound controls run independently of unrelated matrix failures, with their own allow/deny/restore observations. External HTTPS/NTP controls retain the single-exchange behavior described above.

For inbound isolation, the OpenSandbox helper sets source egress and destination ingress independently. Source egress remains allowed while the target changes from allowing the source to denying it. The target's return path stays allowed. This avoids the symmetric-policy mistake of treating an outbound block as inbound isolation.

When the selected backend cannot express independent controls, inbound evidence is `skipped`. Selecting `ingress = "sandbox"` never grants isolation by itself. An explicit `security.accept_insecure = ["inbound_isolation"]` can acknowledge that gap but does not make network-none isolation supported. `external = "any"` never proves unrestricted access to other sandboxes: its policy excludes unlinked sandbox ranges. Sandbox-identity mode never switches to CIDR mode to claim support for `any`.

## DNS and implicit egress

Probe reads resolver addresses and uses `[probe].dns_name` to issue actual DNS packets on TCP and UDP. It compares allowed and none policies and records observed DNS exceptions. A successful DNS query is not a claim that all implicit destinations were enumerated.

`external = "none"` means deny ordinary external access while tolerating measured cluster DNS on TCP/UDP 53; it does **not** mean complete network disconnection. `network_acceptance` requires the external TCP/UDP allow/deny/restore controls and a completed DNS measurement. DNS remaining reachable is an expected platform exception, not a failure. DNS success or an empty exception list alone cannot enable external isolation.

Non-53 TCP/UDP controls on every resolver IP are an optional diagnostic, reported separately as `resolver_non_dns`. Missing controls produce `skipped` without blocking ordinary external isolation; users need not operate listeners on shared DNS infrastructure. When configured, these targets still participate in the external checks, so observed leakage is not ignored. A closed port is not a positive control. A skipped diagnostic does not establish whether resolver exceptions are IP-wide. Exact resolver-IP/port observations remain in the report, and known non-DNS implicit egress is still rejected. Complete disconnection, including DNS, is not established by this acceptance or exposed as a separate supported capability.

This is a bounded behavioral contract over recorded controls, not a proof about arbitrary ports, future topology or undocumented platform exceptions. Retain platform-side policy review and independent acceptance checks where needed.

## Runtime scope and cleanup

Ordinary and privileged paths are measured separately using their configured images/extensions; ordinary evidence does not cover Kata. No automatic runtime/protocol switching occurs. The probe bounds member count and total time, labels all instances, and reconciles possibly lost create responses during cleanup. Review every unconfirmed instance or directory. Finite TTL is a fallback, not deletion confirmation.

`scripts/check_network.py` remains a standalone diagnostic; it cannot generate capability acceptance by declaration. Review its targets and ownership before using it. No real platform validation is implied by offline unit tests.
