# Deployment templates

These files illustrate the input schemas; every field is described in the [configuration reference](../../docs/Configuration.md). They are not tested deployment settings.

For the complete first-time setup, follow the [operator guide](../../docs/Deployment.md). It covers image publication, the first share release, and deployment acceptance.

```sh
mkdir -p deployments/local
cp deployments/examples/opensandbox.py myproject/deployment.py
```

Before running `flotilla probe`:

1. Set the server root and export the credential through the environment variable named in the TOML file. The backend adds `/v1` to the server root.
2. Replace the network CIDRs with the actual sandbox and platform ranges.
3. Provide shared storage: either an allowed host path that is backed by the same storage on every worker node (`volumes = "host"`), or an existing volume that every worker can mount read-write (`volumes = "pvc"`). Create the shared root directory before running the probe.
4. Build and push the anchor and probe images, make them accessible to your deployment, and replace the image digest placeholders. Share publication needs the anchor image already configured.
5. Declare `[platform]`: the listing delay, any TTL cap, label and link limits, and the implicit egress with its resolver IPs. A wrong number here refuses nothing and misbehaves later.
6. Set `[probe]` inputs: pinned ordinary and privileged images, controlled TCP/UDP targets, DNS name and budgets. A missing control is reported as not-shown, never as isolation.
7. Prepare the immutable `share/releases/<release>/` tree, including `bin/busybox`. `publish_release(anchor, name, dir, components={...})` publishes it through `Deployment(PLATFORM).management_anchor()`, a bounded management anchor. Put the release name and the returned `release_sha256` into the spec's `share=`.

Run `flotilla probe --deployment myproject.deployment:PLATFORM`. It reports each isolation property with what it observed and exits nonzero when one did not hold without being waived, or when cleanup was not confirmed. Nothing is written, so there is no report to review later or promote: read the output of the run. The probe makes real sandbox requests only when explicitly invoked, and a failed deletion is reported rather than disguised as a successful TTL cleanup. Keep private deployment inputs outside source control.

See [the backend documentation](../../docs/backends/opensandbox.md) for the implementation and the [deployment guide](../../docs/Deployment.md) for the operator contract.

To assemble the minimal share, copy the verified binary from the anchor image you built:

```sh
mkdir -p share-root/bin
container=$(docker create registry.example.com/team/anchor@sha256:REPLACE_WITH_DIGEST)
docker cp "$container:/.flotilla/bin/busybox" share-root/bin/busybox
docker rm "$container"
chmod 0755 share-root/bin/busybox
```

Retain the binary checksum and distribute its license and corresponding source alongside the image artifacts. Replace the digest placeholder with the `anchor_pin` returned by `flotilla.anchor_build.build_anchor(..., push=True)`.
