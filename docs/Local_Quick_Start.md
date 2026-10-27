# Local quick start

This guide runs a complete Flotilla deployment on one Linux machine: a local image registry, an [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) server using its Docker runtime, and Flotilla itself. It takes about half an hour, most of it image builds and the capability probe. Use it to try Flotilla or to develop against it. It is not an isolated deployment: the configuration below explicitly accepts the security gaps a single Docker host has.

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

The anchor is a small sandbox each Flotilla client keeps for file operations on shared storage. The probe image is what `flotilla probe` tests the platform with. Both scripts print the pushed digest on their last line:

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

Create `$LOCAL/declared.json`. These are your statements about the platform for what the probe cannot measure. The values below are conservative for a single Docker host:

```json
{
  "deployment": "local-docker",
  "max_ttl_seconds": 86400,
  "list_visibility_s": 30,
  "link_max_members": 8,
  "external_forms": ["none", "allowlist"],
  "implicit_egress": ["0.0.0.0/0:*/tcp,udp"],
  "cluster_dns": [],
  "devices": [],
  "link": {"ok": true, "source": "declared", "evidence": "all sandboxes share the default Docker bridge"},
  "link_udp": {"ok": false, "source": "declared", "evidence": "not verified"},
  "inbound_isolation": {"ok": false, "source": "declared", "evidence": "any container on the bridge can reach any sandbox"},
  "implicit_egress_declared": {"ok": true, "source": "declared", "evidence": "conservatively declared as every destination until verified"},
  "shared_volume": {"ok": true, "source": "declared", "evidence": "one local directory bind-mounted into every sandbox"},
  "missing_subpath": {"ok": true, "source": "declared", "evidence": "the Docker runtime creates missing host-path subdirectories"},
  "no_auto_mount": {"ok": false, "source": "declared", "evidence": "the Docker runtime adds its own execd volume"},
  "volume_file": {"ok": true, "source": "declared", "evidence": "the Docker runtime accepts a host file as a bind source"},
  "exec_auth": {"ok": false, "source": "declared", "evidence": "not verified"},
  "diagnostics": {"ok": false, "source": "declared", "evidence": "not assessed"},
  "privileged_runtime": {"ok": false, "source": "declared", "evidence": "not offered"},
  "multi_container": {"ok": false, "source": "declared", "evidence": "not assessed"}
}
```

`list_visibility_s` bounds how long a new sandbox takes to appear in listings; creating a sandbox with its egress sidecar takes a few seconds on the Docker runtime. The [configuration reference](Configuration.md#declaredjson) describes every field.

## 5. Publish the share release

Every sandbox mounts a small, immutable directory of helper tools at `/.flotilla`. Copy BusyBox out of the anchor image and publish it:

```sh
mkdir -p share-root/bin
container=$(docker create localhost:5000/flotilla/anchor@sha256:REPLACE_WITH_ANCHOR_DIGEST)
docker cp "$container:/.flotilla/bin/busybox" share-root/bin/busybox
docker rm "$container"

flotilla share publish share-root --deployment deployment.toml \
  --declared declared.json --release local-1 --component busybox=1.36.1
```

The command prints a `release_sha256` line. Paste it into `[share]` in `deployment.toml`.

## 6. Probe the platform

```sh
flotilla probe --deployment deployment.toml --declared declared.json \
  --image localhost:5000/flotilla/probe@sha256:REPLACE_WITH_PROBE_DIGEST
flotilla share verify --deployment deployment.toml
```

The probe creates real sandboxes and checks lifecycle, execution, timeouts, file transfer, storage, and networking. It takes a few minutes and writes `capabilities.json`. It should end with:

```text
required: all satisfied
security gaps: inbound_isolation, exec_auth, no_auto_mount
```

Those three gaps are the ones `deployment.toml` accepts. If a required item is unmet, the report entry for it says what failed.

## 7. Run a task

The deployment is ready. Run the [README example](../README.md#example), a two-service task with Redis:

```sh
mkdir -p task/environment
printf '[environment]\n' > task/task.toml
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

Save the README's Python example as `example.py` in `$LOCAL` and run it:

```sh
python example.py
# cache: PONG
# main -> cache: +PONG
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
- **Offline tasks.** `implicit_egress` declares every destination, so tasks that ask for no network access (`network_mode = "no-network"`, internal-only networks, Compose `network_mode: none`, or `OpenOptions(disable_internet=True)`) are rejected rather than run with an unverified guarantee. To enable them, verify which destinations sandboxes can reach despite a deny-all policy, typically only the DNS resolver, and declare exactly those. See [Network validation](Network_Validation.md) and the `cluster_dns` field.
- **UDP between services.** `link_udp` is declared false until you check it.

For a deployment that does provide these, follow the [deployment guide](Deployment.md).

## Using sudo

If Docker needs `sudo`, prefix the `docker` commands above with it, pass `DOCKER="sudo docker"` to the two `build.sh` scripts, and tell Flotilla how to run Docker for image preparation:

```toml
[preparation]
docker_command = ["sudo", "-n", "docker"]
```

`sudo -n` fails instead of prompting, so make sure it works without a password, or run `sudo -v` first.
