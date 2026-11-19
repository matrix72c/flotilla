# Local quick start

This is a local infrastructure reference for OpenSandbox's Docker runtime, not evidence that your deployment passes isolation or filesystem contracts. Do not interpret example security waivers as measurements or a no-network guarantee.

Tasks are published as static directories and resolved where they run. The Anchor runs the installed Flotilla helper; no Compose executable, builder or Docker socket is needed anywhere, and a client needs registry read access only. Container `localhost` is not the host registry. Until the capability probe and cleanup pass on your actual deployment, stop before running tasks. See [Deployment](Deployment.md) for installed-package provisioning.

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

Build the native Anchor: a scratch image with the verified BusyBox. For this local HTTP registry, build locally and push through the locally configured Docker daemon:

```sh
python -c 'from flotilla.anchor_build import AnchorBuildOptions, build_anchor
build_anchor(AnchorBuildOptions(image="localhost:5000/flotilla/anchor:local"))'
docker push localhost:5000/flotilla/anchor:local
"$FLOTILLA_REPO/images/probe/build.sh" localhost:5000/flotilla/probe:local --push
```

The separate probe-image script is an optional operator example; a pre-provisioned compatible probe image also works. It needs Python socket/DNS tooling. Record each pushed remote manifest digest, not a local image ID. The native Anchor build and self-test need no framework checkout.

## 4. Write the deployment

Find the address range of Docker's default bridge:

```sh
docker network inspect bridge --format '{{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}'
# 172.17.0.0/16 172.17.0.1
```

Write `$LOCAL/deployment.py`, replacing the bridge subnet and gateway, the shared path, and the two image digests:

```python
from flotilla.config import (
    FlotillaConfig,
    Limits,
    Network,
    OpenSandbox,
    OpenSandboxExecd,
    OpenSandboxNetwork,
    PlatformFacts,
    Probe,
    Reaper,
    Resource,
    ResourceDefaults,
    Security,
    Storage,
)

PLATFORM = FlotillaConfig(
    opensandbox=OpenSandbox(
        endpoint="http://127.0.0.1:8080",
        credential_env="FLOTILLA_OPENSANDBOX_CREDENTIAL",
        network=OpenSandboxNetwork(
            link="cidr",
            sandbox_cidrs=["172.17.0.0/16"],      # bridge subnet
            platform_cidrs=["172.17.0.1/32"],     # bridge gateway
        ),
        execd=OpenSandboxExecd(protocol="current"),
    ),
    storage=Storage(volumes="host", host_path="/path/to/flotilla-local/shared"),
    network=Network(external=[]),                 # sandboxes get no internet access
    # A single Docker host cannot provide these; see "What this setup does not provide".
    security=Security(accept_insecure=["inbound_isolation", "exec_auth", "no_auto_mount"]),
    reaper=Reaper(anchor_image="localhost:5000/flotilla/anchor@sha256:REPLACE_WITH_ANCHOR_DIGEST"),
    limits=Limits(trial_concurrency=4, instance_budget=25),
    resources=ResourceDefaults(default=Resource(cpu="1", memory="1Gi")),
    platform=PlatformFacts(list_visibility_s=30.0),
    probe=Probe(
        image="localhost:5000/flotilla/probe@sha256:REPLACE_WITH_PROBE_DIGEST",
        # Use these services only if approved and reachable for your deployment; there are no
        # built-in targets.
        dns_name="pypi.org",
        external_targets=[
            {"host": "pypi.org", "port": 443, "protocol": "tcp", "service": "https"},
            {"host": "time.cloudflare.com", "port": 123, "protocol": "udp", "service": "ntp"},
        ],
    ),
)
```

A task's images must be published where the sandboxes can pull from, and named by digest.

Nothing here declares what the platform can do: every capability is assumed until a request is refused, and only isolation is measured. An absent control is reported as not shown, never as isolation. Configure TCP/UDP service controls and a DNS query name as described in [Network validation](Network_Validation.md). HTTPS and NTP avoid the need to operate raw echo servers; `service="echo"` remains the default. A standard service uses one pinned IP across allow, deny and allow again, and HTTPS requires valid CA trust and hostname verification. The resolvers named in `platform.cluster_dns` may remain reachable. A waiver records that you run without a property; it does not provide it.

## 5. Measure the platform's isolation

Every sandbox mounts a small, immutable directory of helper tools at `/.flotilla`. The spec sets no `share=`, so the first probe publishes the built-in release from the anchor image's BusyBox.

Commands outside your own process name the spec by where to import it from, so run them with `$LOCAL` on `PYTHONPATH`:

```sh
export PYTHONPATH="$LOCAL"
flotilla probe --deployment deployment:PLATFORM
```

The probe creates real sandboxes and measures whether egress policy, inbound isolation, read-only and subdirectory mounts, auto-mounting and exec authentication actually hold. It prints each observation and writes nothing; a nonzero exit means something it measured did not hold. This guide does not predict which will hold on your Docker runtime. Read the gaps before naming any of them in `security.accept_insecure` — a waiver records that you run without the property, and does not provide it.

## 6. Run a task

Only after the probe exits zero, create the [README example](../README.md#example), a two-service task with Redis:

```sh
mkdir -p task/environment
printf '[environment]\n' > task/task.toml
printf 'Check Redis connectivity.\n' > task/instruction.md
cat > task/environment/docker-compose.yaml <<'EOF'
services:
  main:
    image: alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc   # alpine:3.20
    depends_on:
      cache:
        condition: service_healthy
  cache:
    image: redis@sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe    # redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      retries: 15
EOF
```

Add a verifier script under `task/tests/test.sh` for the task's verification contract. Publish the task directory under a dataset path in `$LOCAL/shared`, preserving file modes, and declare it as `[datasets.example]` with that local root and its platform root. Follow [Quick start](Quick_Start.md) for the exact binding form.

Check the dataset before running anything, which plans each task exactly as `open` would:

```sh
flotilla inspect --deployment deployment:PLATFORM
```

Save the README's Python example as `example.py` in `$LOCAL`, pointing at that dataset, then run:

```sh
python example.py
# PONG
```

Continue with the [quick start](Quick_Start.md), which uses this spec.

## Clean up

A Flotilla client deletes what it created when it exits normally. `flotilla gc` collects what a crashed client left behind. It waits a few minutes between two observations so it never collects a live client:

```sh
flotilla gc --deployment deployment:PLATFORM
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
- **Platform-side building.** Local images and host Docker access do not establish that the management sandbox can compile tasks, reach a build service, or use explicitly mounted credentials.

For a deployment that does provide these, follow the [deployment guide](Deployment.md).

## Using sudo

If host Docker needs `sudo`, prefix the infrastructure commands above or pass `DOCKER="sudo docker"` to the existing build scripts. This does not configure the platform management helper: its Docker command, remote endpoint, network policy and credential paths must be explicitly usable inside the trusted management sandbox. Do not pass host credentials or a privileged Docker socket into agent role sandboxes.
