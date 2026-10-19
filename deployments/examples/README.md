# Deployment templates

These files illustrate the input schemas; every field is described in the [configuration reference](../../docs/Configuration.md). They are not tested deployment settings or evidence of platform capabilities.

For the complete first-time setup, follow the [operator guide](../../docs/Deployment.md). It covers image publication, the first share release before a capability report exists, and deployment acceptance.

```sh
mkdir -p deployments/local
cp deployments/examples/opensandbox.toml deployments/local/deployment.toml
cp deployments/examples/declared.json deployments/local/
```

Before running `flotilla probe`:

1. Set the server root and export the credential through the environment variable named in the TOML file. The backend adds `/v1` to the server root.
2. Replace the network CIDRs with the actual sandbox and platform ranges.
3. Provide shared storage: either an allowed host path that is backed by the same storage on every worker node (`volumes = "host"`), or an existing volume that every worker can mount read-write (`volumes = "pvc"`). Create the shared root directory before running the probe.
4. Build and push the anchor and probe images, make them accessible to your deployment, and replace the image digest placeholders. Share publication needs the anchor image already configured.
5. Review every field in `declared.json`. Its `ok: false` values mean that the template asserts no capability. Set a value to true only after obtaining supporting deployment evidence, and update `evidence` accordingly. Fill in the real TTL, list-visibility bound, and implicit egress targets.
6. Prepare the immutable `share/releases/<release>/` tree, including `bin/busybox`. `flotilla share publish <dir>` assembles it from a local directory: it writes `RELEASE.json`, uploads through a temporary anchor, renames it into place, and prints the two `[share]` lines to paste back. The first publication happens before any capability report exists, so pass `--declared` to bootstrap from the declaration file.

The probe does not automatically verify every declaration. It creates real sandboxes and may leave resources for TTL-based cleanup if platform deletion fails. Keep local settings and generated reports in the Git-ignored `deployments/local/` directory.

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
