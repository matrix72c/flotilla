# Deployment guide

This guide is for whoever operates the sandbox platform. It is done once per deployment; afterwards every Flotilla user only needs the resulting `deployment.toml` and the platform credential.

To try the whole procedure on one machine first, follow the [local quick start](Local_Quick_Start.md).

You need a running [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) server (see its getting-started guide), an image registry that both your build machine and the platform can reach, and shared storage. Field-by-field documentation is in the [configuration reference](Configuration.md).

Provision a dedicated shared-storage root reachable by every sandbox worker and the anchor. Install Docker with BuildKit/buildx on preparation machines. Configure registry credentials through Docker's credential mechanism. Runtime platform credentials come from the environment variable named by `opensandbox.credential_env`.

Copy [the deployment template](../deployments/examples/opensandbox.toml) and [the declaration example](../deployments/examples/declared.json). Replace endpoint, storage, registry, image, and network values. Keep real deployment files and capability reports out of version control; `deployments/local/` is ignored by Git for this purpose.

## Provision once

1. Build and push the anchor with `images/anchor/build.sh registry.example.com/team/anchor --push`. Record the digest. Its controlled BusyBox build must include `stat`, shell, file-management, and networking applets. Pin that digest in `reaper.anchor_image`.
2. Assemble a share directory containing `bin/busybox`. Use the [share layout and scripts](../deployments/examples/README.md); record component versions and their corresponding source/license material.
3. Publish the share using deployment declarations before the first probe:

```sh
flotilla share publish share-root --deployment deployment.toml \
  --declared declared.json --release initial --component busybox=1.36.1
```

Paste the resulting release name and `RELEASE.json` SHA256 into `[share]`. Releases are immutable. Changing bytes requires a new release name.

4. Build and push `images/probe/build.sh registry.example.com/team/probe --push`, then run:

```sh
flotilla probe --deployment deployment.toml --declared declared.json \
  --image registry.example.com/team/probe@sha256:REPLACE_WITH_DIGEST \
  --hosts-recheck 600 --out capabilities.json
flotilla share verify --deployment deployment.toml
```

The digest placeholder must be replaced before use. Review every required item and each security gap. Inbound isolation remains a deployment declaration: an independent client with verified outbound access must test positive and negative controls before the operator supplies acceptance evidence. See [network validation](Network_Validation.md).

## Preparation and budgets

```toml
[prepare]
concurrency = 4
# docker_command = ["sudo", "-n", "docker"]

[build]
pullable_registries = ["registry.example.com"]
target = "registry.example.com/team/tasks"
```

`flotilla prepare` reads `[prepare]` and `[build]`: `prepare.concurrency` bounds how many tasks it builds and publishes at once, on the machine that has Docker and registry access. Opening prepared rows reads neither and needs no Docker. Separately set `limits.trial_concurrency`, `instance_budget`, create/exec rates, and timeouts according to measured platform quotas. Startup reserves replacement capacity; the anchor also consumes an instance.

## Maintain

Public `release()` and `close()` wait for cleanup confirmation up to their deadline. Inspect `CleanupError.report` when cleanup is incomplete; closing a caller does not prove remote deletion. TTL is a final fallback. Use an independent platform client to verify real acceptance runs leave no labeled instances or owned launch directories.

```sh
flotilla gc --deployment deployment.toml
flotilla share gc --deployment deployment.toml --keep previous --dry-run
```

GC is an explicit operator command, not a background schedule. `gc --launch ID` skips the two-observation wait and is appropriate only after confirming that owner has exited. Shared task publications and previous storage formats are never automatically migrated or deleted. Review share GC's dry-run before running it without `--dry-run`.
