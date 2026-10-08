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
3. Provide shared storage that is mounted at the same allowed host path on every worker node.
4. Prepare an immutable `share/releases/<release>/` tree containing `bin/busybox`, set its release name, and calculate the SHA256 of its `RELEASE.json`. The share publication CLI is not implemented yet.
5. Build the anchor and probe images, make them accessible to your deployment, and replace the image digest placeholders.
6. Review every field in `declared.json`. Its `ok: false` values mean that the template asserts no capability. Set a value to true only after obtaining supporting deployment evidence, and update `evidence` accordingly. Fill in the real TTL, list-visibility bound, and implicit egress targets.

The probe does not automatically verify every declaration. It creates real sandboxes and may leave resources for TTL-based cleanup if platform deletion fails. Keep local settings and generated reports in the ignored `deployments/local/` directory.

See [the backend documentation](../../docs/backends/opensandbox.md) for the implementation and [the platform requirements](../../docs/Platform_Requirements.md) for the contract.
