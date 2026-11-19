"""Template only: replace every deployment-specific value before use.

A deployment is a `FlotillaConfig` you construct, so this is ordinary Python -- copy it next to the
rest of your configuration and import `PLATFORM` from your own code. Commands outside that process
name it the same way: `flotilla probe --deployment myproject.deployment:PLATFORM`.
"""

from flotilla.config import (
    FlotillaConfig,
    Limits,
    Network,
    OpenSandbox,
    OpenSandboxExecd,
    OpenSandboxNetwork,
    PlatformFacts,
    Prepare,
    Probe,
    Reaper,
    Resource,
    ResourceDefaults,
    Runtime,
    Security,
    Storage,
    Timeouts,
)

#: Replace with the anchor_pin returned by flotilla.anchor_build.build_anchor(..., push=True).
ANCHOR = "registry.example.com/flotilla@sha256:" + "0" * 64
PROBE = "registry.example.com/team/probe@sha256:" + "0" * 64

PLATFORM = FlotillaConfig(
    opensandbox=OpenSandbox(
        # Server root; the backend appends /v1.
        endpoint="https://sandbox.example.com",
        credential_env="FLOTILLA_OPENSANDBOX_CREDENTIAL",
        # Optional deployment extension: enable only when the backend supports sandboxGroup. It keeps
        # each multi-service trial in one cluster (one generated UUID per trial, not a node pin).
        # group_extension="sandboxGroup",
        network=OpenSandboxNetwork(
            # Default. For a deployment whose IP rules apply only to external addresses, explicitly
            # set link="sandbox" (exact sandbox:<id> peer targets), which supports external=[] and
            # allowlists. If the deployment requires it, enable group_extension above to avoid
            # unsupported cross-cluster peer traffic.
            link="cidr",
            # Default: deployment-provided inbound isolation. If the platform supports sandbox
            # ingress, select link="sandbox" and ingress="sandbox" together instead.
            ingress="deployment",
            sandbox_cidrs=["10.42.0.0/16"],
            platform_cidrs=["10.43.0.0/16", "169.254.169.254/32"],
        ),
        execd=OpenSandboxExecd(protocol="current", wrapper="/.flotilla/bin/busybox"),
    ),
    storage=Storage(
        volumes="host",
        # Must be an allowed host path backed by shared storage on all worker nodes.
        host_path="/srv/flotilla",
        # Alternatively, use an existing volume every worker can mount read-write (a ReadWriteMany
        # PVC on Kubernetes): volumes="pvc", claim_name="flotilla-shared", root_subpath="flotilla".
    ),
    # `share=` is optional: without it, probe and runtime start publish the built-in BusyBox release
    # from the Anchor image. For a custom release, call `flotilla.share.release.publish_release`
    # inside `Deployment(PLATFORM).management_anchor()` and put the release name and the returned
    # `release_sha256` into Share(release=..., release_sha256=...).
    network=Network(external=[]),
    security=Security(accept_insecure=[]),
    reaper=Reaper(anchor_image=ANCHOR, trial_ttl=1800, renew_before_s=600, renew_margin_s=120),
    runtime=Runtime(
        run_timeout_s=21600,
        exec_default_timeout_s=300,
        cancel_confirm_timeout_s=15,
        exec_output_limit_bytes=8388608,
    ),
    limits=Limits(
        exec_timeout=21600,
        exec_concurrency=64,
        exec_rate=500,
        # Set these from measured CPU/memory and sandbox quotas. A three-service run uses 3 live
        # instances and reserves 6 slots for possible startup replacement.
        trial_concurrency=32,
        instance_budget=193,  # 32 * 6 + one anchor, per process
    ),
    timeouts=Timeouts(close=60),
    resources=ResourceDefaults(
        default=Resource(cpu="1", memory="2Gi"),
        # Explicit overrides, by service for every task and by one service of one task:
        # services={"main": Resource(cpu="2", memory="4Gi")},
        # tasks={"example-task": TaskResources(services={"db": Resource(cpu="4", memory="8Gi")})},
    ),
    # Task metadata and resources are read inside the management sandbox, on attempt-private copies.
    prepare=Prepare(concurrency=4),
    # What this platform does that a request cannot discover. Everything else it either does or
    # refuses at create; these are the numbers a request has to be built around.
    platform=PlatformFacts(
        list_visibility_s=30.0,
        # max_ttl_s=21600,
        max_label_value_len=63,
        link_max_members=9,
        # Egress the platform allows regardless of policy, plus the resolver IPs behind port 53.
        # implicit_egress=["10.96.0.10:53/udp,tcp"],
        # cluster_dns=["10.96.0.10"],
    ),
    # Probe inputs are not isolation claims. Replace every digest placeholder and provide controlled
    # TCP/UDP services before relying on the egress measurements.
    probe=Probe(
        deployment="example",
        image=PROBE,
        # privileged_image="registry.example.com/team/dind@sha256:<resolved-64-hex>",
        link_members=9,
        hosts_recheck_s=600.0,
        # dns_name="probe.example.com",
        # external_targets=[
        #     {"host": "192.0.2.10", "port": 48080, "protocol": "tcp"},
        #     {"host": "192.0.2.10", "port": 48080, "protocol": "udp"},
        # ],
    ),
    # Where each published dataset is, on both sides. `local_root` must be absolute.
    # datasets={
    #     "example": DatasetBinding(
    #         local_root="/srv/datasets/example",
    #         platform_root="/srv/flotilla/datasets/example",
    #     )
    # },
)
