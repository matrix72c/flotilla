# Deployment templates

These files illustrate the input schemas; every field is described in the [configuration reference](../../docs/Configuration.md). They are not tested deployment settings or evidence of platform capabilities.

For the complete first-time setup, follow the [operator guide](../../docs/Deployment.md). It covers image publication, the first share release before a capability report exists, and deployment acceptance.

```sh
mkdir -p deployments/local
cp deployments/examples/opensandbox.toml deployments/local/deployment.toml
```

Before running `flotilla probe`:

1. Set the server root and export the credential through the environment variable named in the TOML file. The backend adds `/v1` to the server root.
2. Replace the network CIDRs with the actual sandbox and platform ranges.
3. Provide shared storage: either an allowed host path that is backed by the same storage on every worker node (`volumes = "host"`), or an existing volume that every worker can mount read-write (`volumes = "pvc"`). Create the shared root directory before running the probe.
4. Build and push the anchor and probe images, make them accessible to your deployment, and replace the image digest placeholders. Share publication needs the anchor image already configured.
5. Set `[probe]` inputs: pinned ordinary/privileged/inner images, controlled TCP/UDP echo targets, DNS name and budgets. Missing controls remain non-passed; never fill in manual success flags.
6. Prepare the immutable `share/releases/<release>/` tree, including `bin/busybox`. `flotilla share publish <dir> --deployment deployment.toml` publishes through a bounded management anchor without needing a capability report. Paste the resulting `[share]` release and digest back into the same TOML file.

Run `flotilla probe --deployment deployment.toml --out capabilities.candidate.json`. Review all four-state results and unconfirmed resources before promoting the report. Configuration does not grant capability evidence; there is no declaration file or bootstrap all-capable report. Probe makes real sandbox requests only when explicitly invoked. Failed deletion is reported, not disguised as successful TTL cleanup. Keep private deployment inputs and generated reports outside source control.

See [the backend documentation](../../docs/backends/opensandbox.md) for the implementation and [the platform requirements](../../docs/Platform_Requirements.md) for the contract.

To assemble the minimal share, copy the verified binary from the anchor image you built:

```sh
mkdir -p share-root/bin
container=$(docker create registry.example.com/team/anchor@sha256:REPLACE_WITH_DIGEST)
docker cp "$container:/.flotilla/bin/busybox" share-root/bin/busybox
docker rm "$container"
chmod 0755 share-root/bin/busybox
```

Retain the binary checksum and distribute its license and corresponding source alongside the image artifacts. Replace the digest placeholder with the digest printed by `images/anchor/build.sh --push`.
