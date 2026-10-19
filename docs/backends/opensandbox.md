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

Commands are streamed and tracked using the `init` command ID. A client deadline triggers interruption and bounded status confirmation. A remote fallback deadline protects against a lost client; it does not classify ordinary signal exits as timeouts. Non-idempotent requests are never replayed. See [Runtime API](../Runtime_API.md).

## Storage and network

Storage uses deployment-approved host paths or an existing shared PVC, with confined relative subpaths. The anchor mounts the root; task instances receive only their specified mounts. The controlled anchor image includes BusyBox `stat` for numeric metadata, including setuid/setgid/sticky bits.

`opensandbox.network.link="cidr"` requires accurate sandbox CIDRs. `link="sandbox"` uses explicit `sandbox:<id>` peer targets and supports none/allowlist external policies. The optional `group_extension` is used only when the deployment implements that extension; it groups a trial, rather than pinning a worker node.

Validate the selected peer-target mode against the deployment: support for sandbox-ID policies does not establish support for raw peer-IP/CIDR policies. Record the mode and any required group extension with the capability evidence.

`external="any"` does not mean sandbox subnets are reachable: unlinked sandbox and platform infrastructure ranges remain excluded. Consequently a failed sandbox-to-sandbox request cannot establish inbound isolation. Network test procedure and controls are maintained in [Network validation](../Network_Validation.md).

## Compatibility verification

Select each protocol explicitly and run the opt-in real contracts. Record server/execd image versions and protocol configuration privately with the results. A local mock contract is insufficient evidence for a real gateway's stream, interrupt, upload, or policy behavior.

```sh
python -m tests.contract.foreground --deployment deployment.toml \
  --image "$CONTRACT_IMAGE" --out artifacts/foreground-contract.json
```

The default contract requires three mutually reachable services. For a deployment whose capability report does not support peer links, use `--services 1` to validate execution, cancellation, timeout, binary transfer and cleanup independently. The result records the skipped peer/sidecar checks explicitly; passing this subset does not satisfy multi-service acceptance. Cleanup also confirms deletion of the separate audit anchor.

For a bounded concurrency and renewal check, add `--soak-seconds 120`. Use a private test configuration with `reaper.trial_ttl=120`, `renew_before_s=60`, and `renew_margin_s=20` so service renewal occurs within that window. This phase runs four concurrent commands and binary transfers repeatedly, checks numeric execution identity, and compares service/anchor expiry timestamps through the independent observer. It is a short acceptance check, not a long-duration stress test.
