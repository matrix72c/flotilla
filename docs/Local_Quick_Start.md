# Local quick start

This is a local infrastructure setup reference for OpenSandbox's Docker runtime, not a validated end-to-end recipe for the new platform-owned Harbor flow. The redesign has **not** completed real platform acceptance, and no compatible management image has been published. Old BusyBox-only anchor images cannot run the workspace compiler. Do not interpret the example security waivers as measurements or a no-network guarantee.

The management image must additionally contain Python 3.12, the current Flotilla `harbor` extra, pinned Docker Compose (default 2.39.4), and build tooling. Configure the remote Docker/build endpoint and registry reachability explicitly through `prepare.management_network`; provide credentials only as read-only `management_mounts` and refer to them using nonsecret `management_env` values (for example `DOCKER_HOST` and `DOCKER_CONFIG`). Container `localhost` is not the host registry. No automatic DinD or egress opening is provided. Until these paths and the schema-2 probe pass on your actual deployment, stop before running tasks.

## Prerequisites

- Linux x86_64 with a kernel that has nf_tables. OpenSandbox's egress sidecar enforces network policies with nftables.
- Docker with buildx, able to pull from Docker Hub. The commands use `docker`; if you need `sudo`, see [Using sudo](#using-sudo).
- Python 3.12 and Flotilla installed (`python -m pip install .` in a checkout).
- `curl` and `openssl`.

Everything goes into one working directory:

```sh
export FLOTILLA_REPO=$PWD             # the Flotilla checkout
export LOCAL=$HOME/flotilla-local
mkdir -p "$LOCAL/shared" "$LOCAL/opensandbox"
cd "$LOCAL"
```

`$LOCAL/shared` is the shared storage. The OpenSandbox server bind-mounts subdirectories of it into sandboxes, so it must have the same path inside and outside the server container.

## 1. Start a registry

Flotilla pushes the images it builds to a registry the platform can pull from. Docker treats `localhost` registries as trusted, so a plain local registry works:

```sh
docker run -d --name flotilla-registry -p 127.0.0.1:5000:5000 registry:2
```

## 2. Start OpenSandbox

Write the server configuration to `$LOCAL/opensandbox/sandbox.toml`:

```toml
[server]
host = "127.0.0.1"
port = 8080
max_sandbox_timeout_seconds = 86400

[proxy]
resolve_internal = true

[runtime]
type = "docker"
execd_image = "opensandbox/execd:release-1.1.1"

[docker]
network_mode = "bridge"        # network policies need bridge networking
publish_host = "127.0.0.1"
port_range_min = 40000
port_range_max = 41000

[ingress]
mode = "direct"

[egress]
image = "opensandbox/egress:release-1.1.1"
mode = "dns+nft"

[storage]
allowed_host_paths = ["/path/to/flotilla-local/shared"]   # replace with the value of $LOCAL/shared

[store]
type = "sqlite"
path = "/var/lib/opensandbox/opensandbox.db"
```

Generate an API key and start the server on the host network, with access to the Docker socket and the shared directory:

```sh
openssl rand -hex 32 > api_key && chmod 600 api_key
docker run -d --name flotilla-opensandbox --network host \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$LOCAL/opensandbox/sandbox.toml:/etc/opensandbox/config.toml:ro" \
  -v "$LOCAL/shared:$LOCAL/shared" \
  -v flotilla-opensandbox-state:/var/lib/opensandbox \
  -e OPENSANDBOX_SERVER_API_KEY="$(cat api_key)" \
  opensandbox/server:release-1.1.1
until curl -sf http://127.0.0.1:8080/health; do sleep 1; done
```

Flotilla reads the same key from an environment variable. Export it in every shell you run Flotilla from:

```sh
export FLOTILLA_OPENSANDBOX_CREDENTIAL="$(cat "$LOCAL/api_key")"
```

## 3. Build the anchor and probe images

The historical anchor is a small file-operation sandbox. These scripts build the existing images, not a validated platform-build management image; extend and test the management image requirements above before assigning its digest to `reaper.anchor_image`. The probe image also needs Python socket/DNS tooling for schema-2 checks. Both scripts print the pushed digest on their last line:

```sh
"$FLOTILLA_REPO/images/anchor/build.sh" localhost:5000/flotilla/anchor:local --push
"$FLOTILLA_REPO/images/probe/build.sh" localhost:5000/flotilla/probe:local --push
```

Note both `localhost:5000/...@sha256:...` references.

## 4. Write the deployment

Find the address range of Docker's default bridge:

```sh
docker network inspect bridge --format '{{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}'
# 172.17.0.0/16 172.17.0.1
```

Create `$LOCAL/deployment.toml`, replacing the bridge subnet and gateway, the shared path, and the anchor digest:

```toml
[opensandbox]
endpoint = "http://127.0.0.1:8080"
credential_env = "FLOTILLA_OPENSANDBOX_CREDENTIAL"
capabilities = "capabilities.json"

[opensandbox.network]
link = "cidr"
sandbox_cidrs = ["172.17.0.0/16"]     # bridge subnet
platform_cidrs = ["172.17.0.1/32"]    # bridge gateway

[opensandbox.execd]
protocol = "current"

[storage]
volumes = "host"
host_path = "/path/to/flotilla-local/shared"

[share]
release = "local-1"
release_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"  # filled in by step 5

[network]
external = []                         # sandboxes get no internet access

[security]
# A single Docker host cannot provide these; see "What this setup does not provide".
accept_insecure = ["inbound_isolation", "exec_auth", "no_auto_mount"]

[reaper]
anchor_image = "localhost:5000/flotilla/anchor@sha256:REPLACE_WITH_ANCHOR_DIGEST"

[limits]
trial_concurrency = 4
instance_budget = 25

[resources.default]
cpu = "1"
memory = "1Gi"

[build]
target = "localhost:5000/flotilla/tasks"
pullable_registries = ["localhost:5000", "docker.io"]
```

`[build].pullable_registries` lists registries the sandboxes can pull from directly. Images from anywhere else are copied to `[build].target` first.

Add probe inputs to the same `deployment.toml`:

```toml
[probe]
deployment = "local-docker"
image = "localhost:5000/flotilla/probe@sha256:REPLACE_WITH_PROBE_DIGEST"
link_members = 9
list_visibility_s = 30
# Use these services only if approved/reachable for your deployment; no built-in targets.
dns_name = "pypi.org"
external_targets = [
  { host = "pypi.org", port = 443, protocol = "tcp", service = "https" },
  { host = "time.cloudflare.com", port = 123, protocol = "udp", service = "ntp" },
]
```

There is no handwritten declaration file. A probe reports `passed`, `failed`, `skipped`, or `error`; absent controls never count as success. Configure TCP/UDP service controls and a DNS query name as described in [Network validation](Network_Validation.md). HTTPS and NTP avoid the need to operate raw echo servers; `service = "echo"` remains the default for existing controls. Standard services use one pinned IP across allow/deny/restore, and HTTPS requires valid CA trust and hostname verification. Cluster DNS may remain reachable; resolver-IP non-53 controls are optional diagnostics, not a prerequisite. Explicit security waivers cannot manufacture functional or no-network acceptance.

## 5. Publish the share release

Every sandbox mounts a small, immutable directory of helper tools at `/.flotilla`. Copy BusyBox out of the anchor image and publish it:

```sh
mkdir -p share-root/bin
container=$(docker create localhost:5000/flotilla/anchor@sha256:REPLACE_WITH_ANCHOR_DIGEST)
docker cp "$container:/.flotilla/bin/busybox" share-root/bin/busybox
docker rm "$container"

flotilla share publish share-root --deployment deployment.toml \
  --release local-1 --component busybox=1.36.1
```

The command prints a `release_sha256` line. Paste it into `[share]` in `deployment.toml`.

## 6. Probe the platform

```sh
flotilla probe --deployment deployment.toml
flotilla share verify --deployment deployment.toml
```

The probe creates real sandboxes and checks lifecycle, execution, authentication, storage, and controlled networking. It atomically writes `capabilities.json`, including cleanup status and configuration binding. Inspect every required result and cleanup outcome; this guide does not predict which checks will pass on your Docker runtime. Missing required evidence blocks runtime admission. Review security gaps before accepting any by name; never edit report outcomes to bypass admission.

## 7. Run a task

Only after management connectivity, capability checks and cleanup have been validated, create the [README example](../README.md#example), a two-service task with Redis:

```sh
mkdir -p task/environment
printf '[environment]\n' > task/task.toml
printf 'Check Redis connectivity.\n' > task/instruction.md
cat > task/environment/docker-compose.yaml <<'EOF'
services:
  main:
    image: alpine:3.20
    depends_on:
      cache:
        condition: service_healthy
  cache:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      retries: 15
EOF
```

Stage this complete original task at `datasets/demo/task` below `$LOCAL/shared` using storage tooling that preserves modes. Save the README's Python example as `example.py` in `$LOCAL`; the client passes only that relative key. Once acceptance is complete, run it:

```sh
python example.py
# PONG
```

Continue with the [quick start](Quick_Start.md), which uses this `deployment.toml`.

## Clean up

A Flotilla client deletes what it created when it exits normally. `flotilla gc` collects what a crashed client left behind. It waits a few minutes between two observations so it never collects a live client:

```sh
flotilla gc --deployment deployment.toml
docker ps -a --filter name=sandbox-     # should list nothing
```

To remove the local deployment:

```sh
docker rm -f flotilla-opensandbox flotilla-registry
docker volume rm flotilla-opensandbox-state
sudo rm -rf "$LOCAL"   # sandboxes create root-owned files under shared/
```

## What this setup does not provide

- **Inbound isolation.** Any container on the Docker bridge can connect to any sandbox, including sandboxes of other environments.
- **Verified exec authentication.** Flotilla gives each sandbox's exec daemon an access token, but its ports are published on the host and this setup has not verified that requests without the token are rejected.
- **Offline tasks.** Only measured network acceptance with controlled TCP/UDP and DNS behavior can authorize them. An empty exception list is not evidence; there is no declaration-based shortcut. See [Network validation](Network_Validation.md).
- **UDP between services.** Enabled only after directed content-bearing UDP and disconnect/reconnect checks pass.
- **Platform-side building.** The historical local images and host Docker access do not establish that the management sandbox can compile tasks, reach a build service, or use explicitly mounted credentials.

For a deployment that does provide these, follow the [deployment guide](Deployment.md).

## Using sudo

If host Docker needs `sudo`, prefix the infrastructure commands above or pass `DOCKER="sudo docker"` to the existing build scripts. This does not configure the platform management helper: its Docker command, remote endpoint, network policy and credential paths must be explicitly usable inside the trusted management sandbox. Do not pass host credentials or a privileged Docker socket into agent role sandboxes.
