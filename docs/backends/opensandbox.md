# OpenSandbox backend

Set `opensandbox.endpoint` to the server root; the backend appends `/v1`. Credentials are read from `opensandbox.credential_env`. Each runtime owns its HTTP clients and closes them explicitly. Error messages retain operation stage, HTTP status, and constrained error codes/request IDs, excluding raw response bodies.

Authentication and other non-retryable failures stop readiness/reconciliation polling. A rejected cleanup request remains unconfirmed; it never counts as a successful deletion. A definitively rejected create needs no reconciliation, while a lost create response or a deadline during creation still does.

## execd protocols

| Setting | `current` | `legacy` |
|---|---|---|
| Command representation | argv and numeric identity fields | Quoted command string; wrapper applies identity/environment |
| File routes | `/v1/filesystem/{uid}/{gid}/...` | `/files/...` |
| Upload ownership | Route selects writing identity | Root writes, then chown and chmod restore requested metadata |
| Literal upload paths | Identity route contract | Absolute path without `$`; remote expansion is rejected |

Choose the deployment's actual protocol in `[opensandbox.execd]`; there is no speculative retry using a different protocol. Both paths use the configured BusyBox wrapper (default `/.flotilla/bin/busybox`). Task images must provide the shell/tools required by their own commands and build steps; Flotilla does not inject a missing `/bin/sh` into arbitrary images.

The exec daemon runs as root so it can initialize the sandbox and apply each command's numeric identity. Preparation captures the source image's `USER`, command, working directory, environment, and healthcheck before producing a runtime image with `USER 0:0`. Service startup, healthchecks, and default executions retain the original task identity; Compose `user` still overrides it. Source images are unchanged. Images already using the default root identity need no extra build. Other images, including otherwise linked or mirrored images, require a builder and `[build].target` for this small derived image. The deployment must also permit the daemon to run as root.

Commands are streamed and tracked using the `init` command ID. A client deadline triggers interruption and bounded status confirmation. A remote fallback deadline protects against a lost client; it does not classify ordinary signal exits as timeouts. Non-idempotent requests are never replayed. See [Runtime API](../Runtime_API.md).

An environment health check retries a confirmed timeout of its internal `true` probe within `runtime.check_timeout_s`, as it does transient transport failures. A nonzero probe result, unknown execution outcome, or exhaustion of the overall check budget still fails closed. This does not retry user commands or increase their deadlines.

For the command stream only, HTTP 409 with error code `CONFLICT` (flat or inside `detail`) is recognized as the deployment's explicit busy rejection and raised as `CommandBusy` (`transient`, retryable). It is not converted to `ExecOutcomeUnknown`, and neither foreground nor background commands are automatically replayed. Healthchecks skip this round without consuming a failure and probe again on their next interval, within the startup stage's overall deadline. Bare 409 responses, other error codes, and conflicts on other routes keep their existing classification. Transport loss and incomplete foreground results still report unknown outcomes.

To investigate command conflicts, enable DEBUG on the Python logger `flotilla.platform.opensandbox.http` and configure a logging handler to retain those records. Each stream logs send, response headers, `init` when observed, and request end with a shared local `trace_id`. Records include the sandbox proxy path, stage, background flag, monotonic timestamps, HTTP status, constrained platform request/command IDs, and total duration. Failed or cancelled requests also emit their end record at WARNING. Compare send/end intervals for the same sandbox within a client process to find overlapping requests; monotonic timestamps from different processes or hosts are not a shared timeline. `response_complete` means the HTTP stream closed, not that the command succeeded. Payloads, environment values, output, credentials, and raw error bodies are excluded. No per-instance serialization is added.

## Storage and network

Storage uses deployment-approved host paths or an existing shared PVC, with confined relative subpaths. The anchor mounts the root; task instances receive only their specified mounts. The controlled anchor image includes BusyBox `stat` for numeric metadata, including setuid/setgid/sticky bits.

`opensandbox.network.link="cidr"` requires accurate sandbox CIDRs. `link="sandbox"` uses explicit `sandbox:<id>` peer targets and supports none/allowlist external policies. The optional `group_extension` is used only when the deployment implements that extension; it groups a trial, rather than pinning a worker node.

Validate the selected peer-target mode against the deployment: support for sandbox-ID policies does not establish support for raw peer-IP/CIDR policies. Record the mode and any required group extension with the capability evidence.

Create uses the common top-level `networkPolicy` field; replacements use `PUT /v1/sandboxes/{id}/networkpolicy` on both upstream OpenSandbox and platforms implementing its compatibility route. Flotilla does not fall back to a provider-specific route or the sidecar API. Upstream's policy schema contains `defaultAction` and `egress`; provider extensions are explicitly selected.

On an internal platform supporting sandbox ingress, set both `opensandbox.network.link="sandbox"` and `opensandbox.network.ingress="sandbox"`. Every instance starts with `ingress={"defaultAction":"allow","rules":[{"action":"deny","from":"sandbox:any","protocol":"ANY"}]}`. Explicit `protocol="ANY"` covers every protocol; the platform defaults an omitted protocol to TCP. Linking replaces both egress and ingress, adding only that service's exact peers to the ingress rule's `except` list. Services with Compose `network_mode: none` stay unlinked and keep the initial deny rule. Platform ingress, execd, and probes remain available. With the default `ingress="deployment"`, no ingress extension is sent; Compose `none` requires the recorded inbound isolation capability, even when the deployment otherwise accepts an inbound-isolation security waiver.

No-network policies use `defaultAction="deny"` with no external allow rules. The only tolerated implicit egress is TCP/UDP 53 to the capability report's explicit `cluster_dns` IPs. Reports and probe declarations retain all `implicit_egress` entries; older reports without `cluster_dns` conservatively reject any implicit egress for offline tasks. Update declarations with resolver evidence and canonical destination strings, rather than deleting exceptions. Enabling an ingress extension does not promote old declarations to probed evidence; verify positive and negative connectivity controls against the actual deployment.

`external="any"` does not mean sandbox subnets are reachable: unlinked sandbox and platform infrastructure ranges remain excluded. Consequently a failed sandbox-to-sandbox request cannot establish inbound isolation. Network test procedure and controls are maintained in [Network validation](../Network_Validation.md).

## Compatibility verification

Select each protocol explicitly and run the opt-in real contracts. Record server/execd image versions and protocol configuration privately with the results. A local mock contract is insufficient evidence for a real gateway's stream, interrupt, upload, or policy behavior.

```sh
python -m tests.contract.foreground --deployment deployment.toml \
  --image "$CONTRACT_IMAGE" --out artifacts/foreground-contract.json
```

The default contract requires three mutually reachable services. For a deployment whose capability report does not support peer links, use `--services 1` to validate execution, cancellation, timeout, binary transfer and cleanup independently. The result records the skipped peer/sidecar checks explicitly; passing this subset does not satisfy multi-service acceptance. Cleanup also confirms deletion of the separate audit anchor.

For a bounded concurrency and renewal check, add `--soak-seconds 120`. Use a private test configuration with `reaper.trial_ttl=120`, `renew_before_s=60`, and `renew_margin_s=20` so service renewal occurs within that window. This phase runs four concurrent commands and binary transfers repeatedly, checks numeric execution identity, and compares service/anchor expiry timestamps through the independent observer. It is a short acceptance check, not a long-duration stress test.
