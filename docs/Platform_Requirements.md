# Platform requirements and acceptance

The supported release target is Linux x86_64, Python 3.12, Harbor 0.24.0, and the two OpenSandbox execd protocols. Supporting the Python API does not establish that a particular sandbox deployment satisfies the security contract.

| Area | Implementation | Required acceptance |
|---|---|---|
| Creation and ownership | Labels, bounded requests, uncertain-create reconciliation, renewable TTL | Create/get/list/delete consistency and list visibility; independent cleanup observation |
| Execution | Explicit argv, cwd, environment and numeric uid/gid; foreground and background | Identity, nonzero/signal exits, bounded output, cancellation at every request/delivery stage |
| Timeout | Client deadline plus confirmed remote stop | Stop process group and children; unknown status must invalidate environment |
| Files | Binary transfers; canonical entries with original uid/gid/mode | Arbitrary multipart payloads, numeric metadata, special permission bits, concurrent source mutation |
| Shared storage | Host shared root or deployment-supported PVC subpaths | Same data on every worker, read-only mounts, subpath isolation, no unintended mounts |
| Network | Explicit none/allowlist/any policies; CIDR or sandbox peer links | Positive and negative communication controls, internal-only groups, UDP when declared |
| Inbound isolation | **Declared**, never inferred from a peer's failed outbound connection | Independent source with verified allowed outbound access must reach a positive control and fail the isolated target |
| Security waivers | Explicit accepted gaps; stale waivers warn | Record only configured waivers intersecting current gaps |
| Resources | CPU/memory defaults and per-service overrides | Actual cgroup limits and quota behavior |
| Images | Digest resolution; static FROM substitution; linux/amd64 selection | BuildKit and runtime pull access tested independently; reject missing supported architecture |
| Harbor | Main/shared/separate verifier, overrides, logs, borrowed/owned lifecycle | Pinned Harbor contracts plus Oracle/Nop and cleanup checks |
| Unsupported | Root filesystem read-only, devices, extra hosts, `on-failure` restarts, other unsupported Compose keys (see [Compose support](Compose_Support.md)) | Field-level rejection before runtime creation |

## Evidence

Capability items have a source (`probed` or `declared`) and evidence. A declared item is an operator assertion, not an automatic test result. `probe` measures only its implemented checks; an interrupted or failed probe is not evidence that a capability is absent or present. The example declaration asserts no unverified security capabilities.

The capability report is deployment-specific and must match its endpoint. Keep real reports private. Missing required execution/storage/lifecycle capabilities block startup. Security gaps block startup unless explicitly accepted; acceptance does not implement the missing protection.

## Formal release gates

Both `current` and `legacy` must independently pass source preparation, foreground/background execution, timeout and repeated cancellation, binary transfer, service networking, and cleanup checks. Run with an independent observer to verify instances and owned directories disappear. Also verify real Docker archive metadata, prepare/cleanup saturation, and a prolonged renewal/cleanup workload.

Pass unit tests from a clean checkout and extracted source archive, base wheel installation without Harbor, and the Harbor extra. Inspect the release commit, wheel, sdist, and image contexts for private material and excluded modules. Any change after acceptance must repeat the affected checks before the release is tagged; see [RELEASING.md](../RELEASING.md).
