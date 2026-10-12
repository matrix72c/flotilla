# Deployment templates

These files illustrate the input schemas. They are not tested deployment settings or evidence of platform capabilities.

```sh
mkdir -p deployments/local
cp deployments/examples/opensandbox.toml deployments/local/
cp deployments/examples/declared.json deployments/local/
```

Before running `flotilla probe`:

1. Set the server root and export the credential through the environment variable named in the TOML file. The backend adds `/v1` to the server root.
2. Replace the network CIDRs with the actual sandbox and platform ranges.
3. Provide shared storage: either an allowed host path that is backed by the same storage on every worker node (`volumes = "host"`), or an existing volume that every worker can mount read-write (`volumes = "pvc"`). Create the shared root directory before running the probe.
4. Prepare the immutable `share/releases/<release>/` tree. `flotilla share publish <dir>` assembles it from a local directory: it writes `RELEASE.json`, uploads through a temporary anchor, renames it into place, and prints the two `[share]` lines to paste back. The first publication happens before any capability report exists, so pass `--declared` to bootstrap from the declaration file.
5. Build the anchor and probe images, make them accessible to your deployment, and replace the image digest placeholders.
6. Review every field in `declared.json`. Its `ok: false` values mean that the template asserts no capability. Set a value to true only after obtaining supporting deployment evidence, and update `evidence` accordingly. Fill in the real TTL, list-visibility bound, and implicit egress targets.

The probe does not automatically verify every declaration. It creates real sandboxes and may leave resources for TTL-based cleanup if platform deletion fails. Keep local settings and generated reports in the ignored `deployments/local/` directory.

See [the backend documentation](../../docs/backends/opensandbox.md) for the implementation and [the platform requirements](../../docs/Platform_Requirements.md) for the contract.
