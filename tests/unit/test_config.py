"""`FlotillaConfig`: load validation, isolation evidence checks, core/backend translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from flotilla.config import ConfigError, FlotillaConfig
from flotilla.platform.base import ExternalPolicy
from flotilla.testing.config import SHA, config, config_data
from flotilla.testing.reports import evidence, no

# ───────────────────────────── Loading ─────────────────────────────


def test_a_spec_is_constructed_not_parsed() -> None:
    """Backend settings reach the backend from the object a caller built, with no file in between."""
    cfg = FlotillaConfig.from_mapping(
        {
            "opensandbox": {
                "endpoint": "http://os.test",
                "credential_env": "FLOTILLA_CRED",
                "network": {"sandbox_cidrs": ["10.0.0.0/16"]},
                "create_fields": {"ports": [{"containerPort": 44772, "name": "execd", "protocol": "tcp"}]},
            },
            "storage": {"volumes": "host", "host_path": "/srv/flotilla"},
            "share": {"release": "2026-10-07.1", "release_sha256": SHA},
        }
    )
    assert cfg.opensandbox_settings().create_fields["ports"][0]["containerPort"] == 44772
    assert cfg.storage_settings().host_path == "/srv/flotilla"
    assert cfg.network.policy() == ExternalPolicy(mode="none")  # no external given: only none


def test_a_deployment_is_named_by_where_to_import_it_from() -> None:
    """`flotilla gc` has only a name to go on, so a name has to reach the same spec the client used."""
    from flotilla.testing.config import importable

    cfg = config()
    assert FlotillaConfig.load(importable(cfg)) == cfg
    for target in ("flotilla.testing.config", ":DEPLOYMENT", "flotilla.testing.config:"):
        with pytest.raises(ConfigError, match=r"package\.module:attribute"):
            FlotillaConfig.load(target)
    with pytest.raises(ConfigError, match="cannot import"):
        FlotillaConfig.load("flotilla.no_such_module:DEPLOYMENT")
    with pytest.raises(ConfigError, match="defines no ABSENT"):
        FlotillaConfig.load("flotilla.testing.config:ABSENT")
    with pytest.raises(ConfigError, match="not a FlotillaConfig"):
        FlotillaConfig.load("flotilla.testing.config:SHA")


def test_a_deployment_may_be_named_as_a_factory() -> None:
    """A recipe that builds its spec from arguments exposes the function, not a module global."""
    from flotilla.testing.config import importable

    cfg = config()
    target = importable(cfg)
    module_name, _, _ = target.partition(":")
    import sys

    sys.modules[module_name].build = lambda: cfg  # type: ignore[attr-defined]
    assert FlotillaConfig.load(f"{module_name}:build") == cfg


def test_group_extension_is_optional() -> None:
    assert config().opensandbox_settings().group_extension is None
    data = config_data()
    data["opensandbox"] = {**data["opensandbox"], "group_extension": "sandboxGroup"}
    assert FlotillaConfig.from_mapping(data).opensandbox_settings().group_extension == "sandboxGroup"


def test_pvc_storage_translates_to_settings() -> None:
    cfg = config(storage={"volumes": "pvc", "claim_name": "project-data", "root_subpath": "opt/flotilla"})
    settings = cfg.storage_settings()
    assert (settings.volumes, settings.claim_name, settings.root_subpath) == ("pvc", "project-data", "opt/flotilla")


def test_endpoint_trailing_slash_normalized() -> None:
    assert config().opensandbox_settings().endpoint == "http://os.test"


@pytest.mark.parametrize(
    "sections",
    [
        {"unknown": {}},  # unknown section
        {
            "opensandbox": {**config_data()["opensandbox"], "credential": "ak:sk"}
        },  # credentials must not be in the config
        {"opensandbox": {**config_data()["opensandbox"], "credential_env": "ak:sk"}},
        {"opensandbox": {**config_data()["opensandbox"], "endpoint": "os.test"}},
        {"storage": {"volumes": "unknown", "host_path": "/mnt"}},  # unsupported volume form
        {"storage": {"volumes": "pvc", "host_path": "/mnt"}},  # pvc form without claim_name
        {"share": {"release": "../x", "release_sha256": SHA}},
        {"share": {"release": "r1", "release_sha256": "abc"}},
        {"network": {"external": ["0.0.0.0/0"]}},  # unrestricted egress must be written as "any"
        {"network": {"external": ["any"]}},  # "any" inside a list is not unrestricted egress
        {"network": {"external": ["1.2.3"]}},  # a truncated IP is not a domain name
        {"network": {"external": ["*.com"]}},  # wildcard over a whole TLD
        {"network": {"external": ["example.-com-"]}},
        {"network": {"external": ["https://gateway"]}},
        {"network": {"external": "all"}},
        {"reaper": {"renew_interval": 60, "grace": 90}},  # renewal interval not "clearly smaller" than G
        {"reaper": {"renew_interval": 2, "grace": 90}},  # shorter than the anchor's renewal-failure retry interval
        {"reaper": {"grace": 90, "gc_window": 60}},
        {"limits": {"exec_rate": 0}},
    ],
)
def test_rejects_invalid_config(sections: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        config(**sections)


def test_any_requires_platform_cidrs() -> None:
    sandbox_only = {**config_data()["opensandbox"], "network": {"sandbox_cidrs": ["10.0.0.0/16"]}}
    with pytest.raises(ValidationError, match="platform_cidrs"):
        config(opensandbox=sandbox_only, network={"external": "any"})


def test_cidr_link_requires_sandbox_cidrs() -> None:
    with pytest.raises(ValidationError, match="sandbox_cidrs"):
        config(opensandbox={**config_data()["opensandbox"], "network": {}}, network={"external": []})


def test_sandbox_link_is_explicit_and_translated_without_sandbox_cidrs() -> None:
    assert config().network_settings().link == "cidr"
    cfg = config(
        opensandbox={**config_data()["opensandbox"], "network": {"link": "sandbox"}},
        network={"external": ["gw.example.com", "10.0.0.5/32"]},
    )
    assert cfg.network_settings().link == "sandbox"
    assert cfg.network_settings().sandbox_cidrs == ()


def test_sandbox_link_rejects_unrestricted_external_before_requests() -> None:
    with pytest.raises(ValidationError, match=r"sandbox.*any"):
        config(
            opensandbox={**config_data()["opensandbox"], "network": {"link": "sandbox"}},
            network={"external": "any"},
        )


def test_credential_from_environment_only() -> None:
    cfg = config()
    assert cfg.credential({"FLOTILLA_CRED": "ak:sk"}) == "ak:sk"
    with pytest.raises(ConfigError, match="FLOTILLA_CRED") as exc:
        cfg.credential({})
    assert "ak:sk" not in str(exc.value)


# ───────────────────────────── Checks against the isolation evidence ─────────────────────────────


def test_fully_isolated_evidence_passes() -> None:
    config().check_isolation(evidence())


def test_collects_all_problems() -> None:
    """One call reports every missing property, so an operator sees the whole gap at once."""
    with pytest.raises(ConfigError) as exc:
        config().check_isolation(evidence(exec_auth=no(), read_only=no(), cleanup=no()))
    problems = exc.value.problems
    assert any("exec_auth" in p and "accept_insecure" in p for p in problems)
    assert any("read_only" in p for p in problems)
    assert any("cleanup" in p for p in problems)


def test_an_unexercised_external_form_is_a_problem() -> None:
    """The probe allows and denies traffic for a form; a form it never tried proves nothing."""
    with pytest.raises(ConfigError, match="allowlist"):
        config().check_isolation(evidence(external_forms=frozenset({"none", "any"})))
    config(network={"external": []}).check_isolation(evidence(external_forms=frozenset()))


def test_security_gaps_require_explicit_acceptance_and_no_stale_entries() -> None:
    gap = evidence(no_auto_mount=no())
    config(security={"accept_insecure": ["no_auto_mount"]}).check_isolation(gap)
    with pytest.raises(ConfigError, match="no_auto_mount"):
        config().check_isolation(gap)
    config(security={"accept_insecure": ["exec_auth"]}).check_isolation(evidence())


def test_gc_window_must_exceed_grace_plus_declared_visibility() -> None:
    """The listing budget is a platform fact, so the timing is settled at load, not at probe time."""
    with pytest.raises(ValidationError, match="gc_window"):
        config(platform={"list_visibility_s": 120.0})
    cfg = config(platform={"list_visibility_s": 120.0}, reaper={**config_data()["reaper"], "gc_window": 300})
    assert cfg.platform_capabilities().list_visibility_s == 120.0


def test_anchor_ttl_must_fit_a_declared_platform_cap() -> None:
    with pytest.raises(ValidationError, match="max_ttl_s"):
        config(platform={"max_ttl_s": 300}, reaper={**config_data()["reaper"], "anchor_ttl": 600})
    assert config(platform={"max_ttl_s": 7200}).platform_capabilities().max_ttl_seconds == 7200
    assert config().platform_capabilities().max_ttl_seconds is None  # uncapped unless declared


@pytest.mark.parametrize(
    ("external", "value", "ok"),
    [
        (["gateway.example.com"], "https://gateway.example.com/v1", True),
        (["*.example.com"], "https://gw.example.com/v1", True),
        (["10.20.0.0/24"], "http://10.20.0.7:8000/v1", True),
        (["gateway.example.com"], "https://other.example.com/v1", False),
        (["10.20.0.0/24"], "http://10.30.0.7/v1", False),
        (["gateway.example.com"], "qwen3-32b", True),  # not address-like: no network involved
        (["10.20.0.0/24"], "10.30.0.7:8000/v1", False),  # addresses without a scheme are checked too
        (["gateway.example.com"], "gateway.example.com:8443", True),
    ],
)
def test_runtime_params_must_be_reachable(external: list[str], value: str, ok: bool) -> None:
    cfg = config(network={"external": external}, runtime_params={"UPSTREAM": value})
    assert cfg.runtime_params["UPSTREAM"] == value


def test_any_rejects_runtime_param_on_platform_infrastructure() -> None:
    cfg = config(network={"external": "any"}, runtime_params={"META": "http://169.254.169.254/latest"})
    assert cfg.runtime_params["META"].endswith("/latest")


# ───────────────────────────── Translation ─────────────────────────────


def test_external_policy_forms() -> None:
    assert config(network={"external": "any"}).network.policy() == ExternalPolicy(mode="any")
    assert config(network={"external": []}).network.policy() == ExternalPolicy(mode="none")
    narrow = config().network.policy()
    assert narrow == ExternalPolicy(mode="allowlist", hosts=("gateway.example.com",), cidrs=("10.20.0.0/24",))


def test_translates_to_core_and_backend_settings() -> None:
    cfg = config(
        timeouts={"create": 300, "close": 5},
        reaper={**config_data()["reaper"], "trial_ttl": 7200},
        opensandbox={**config_data()["opensandbox"], "execd": {"protocol": "legacy"}},
    )
    trial = cfg.trial_settings(extra_labels={"run": "r1"})
    assert trial.share_release == "2026-10-07.1" and trial.create_timeout_s == 300 and trial.ttl_s == 7200
    assert trial.external.mode == "allowlist" and trial.extra_labels == {"run": "r1"}
    assert cfg.reaper_settings().close_timeout_s == 5
    assert cfg.monitor_settings().ttl_s == 7200
    assert cfg.anchor_settings().grace_s == 90 and cfg.anchor_settings().image.startswith("reg/flotilla@")
    assert cfg.execd_settings().protocol == "legacy"
    assert cfg.network_settings().sandbox_cidrs == ("10.0.0.0/16",)
    assert cfg.resources.effective("task", "main", {})["memory"] == "2Gi"


def test_create_request_timeout_is_separate_from_the_stage() -> None:
    cfg = config(timeouts={"create": 300, "create_request": 20})
    assert cfg.trial_settings().create_timeout_s == 300
    assert cfg.reaper_settings().create_request_timeout_s == 20
    with pytest.raises(ValidationError, match="create_request"):
        config(timeouts={"create": 60, "create_request": 60})


def test_pending_limit_is_below_the_create_stage() -> None:
    assert config(timeouts={"create": 900, "pending": 120}).trial_settings().pending_timeout_s == 120
    assert config().trial_settings().pending_timeout_s == 300
    assert config(timeouts={"create": 300}).trial_settings().pending_timeout_s is None  # default not below create
    with pytest.raises(ValidationError, match=r"timeouts\.pending"):
        config(timeouts={"create": 300, "pending": 300})


@pytest.mark.parametrize(
    "timeouts,expected",
    [
        ({}, 300),
        ({"create": 180}, None),
        ({"create": 300}, None),
        ({"create": 900}, 300),
        ({"create": 180, "pending": 90}, 90),
        ({"create": 180, "pending": None}, None),
    ],
)
def test_pending_timeout_full_snapshot_roundtrip(timeouts: dict[str, float | None], expected: float | None) -> None:
    cfg = config(timeouts=timeouts)
    snapshot = cfg.model_dump(mode="json")
    restored = FlotillaConfig.model_validate(snapshot)
    assert restored.timeouts.pending == expected
    assert restored.trial_settings().pending_timeout_s == cfg.trial_settings().pending_timeout_s == expected
    assert restored.model_dump(mode="json") == snapshot


def test_anchor_image_required_for_anchor_settings() -> None:
    with pytest.raises(ConfigError, match="anchor_image"):
        config(reaper={}).anchor_settings()


@pytest.mark.parametrize("ttl", [1, 180, 1800, 21600])
def test_configured_trial_ttl_can_start_monitor(ttl: int) -> None:
    cfg = config(reaper={**config_data()["reaper"], "trial_ttl": ttl})
    settings = cfg.monitor_settings()
    assert settings.ttl_s == cfg.trial_settings().ttl_s == ttl
    assert 0 < settings.renew_margin_s < settings.renew_before_s < ttl
    if ttl == 21600:
        assert (settings.renew_before_s, settings.renew_margin_s) == (600, 120)


@pytest.mark.parametrize(
    "section",
    [
        {"opensandbox": {**config_data()["opensandbox"], "credential": "AK:SECRET"}},
        {"opensandbox": {**config_data()["opensandbox"], "credential_env": "AK:SECRET"}},
        {"credential": "AK:SECRET"},
    ],
)
def test_validation_errors_never_echo_values(section: dict[str, Any]) -> None:
    # The most common misplacements of a credential: an unknown field or credential_env itself. Error messages carry
    # only the field path and reason (principle 8).
    with pytest.raises(ValidationError) as exc:
        config(**section)
    assert "SECRET" not in str(exc.value)


def test_any_rejects_runtime_param_inside_sandbox_cidrs() -> None:
    cfg = config(network={"external": "any"}, runtime_params={"PEER": "http://10.0.3.4:8000"})
    assert cfg.runtime_params["PEER"].startswith("http://10.0.3.4")


def test_unknown_link_mode_is_rejected() -> None:
    data = config_data()
    data["opensandbox"]["network"]["link"] = "automatic"
    with pytest.raises(ValueError, match="cidr"):
        FlotillaConfig.from_mapping(data)


def test_platform_specific_target_is_not_an_external_hostname() -> None:
    data = config_data()
    data["network"]["external"] = ["sandbox:some-instance"]
    with pytest.raises(ValueError, match="IP / CIDR"):
        FlotillaConfig.from_mapping(data)


def test_legacy_resource_role_defaults_are_rejected() -> None:
    with pytest.raises(ValueError, match="defaults"):
        config(resources={"defaults": {"agent": {"cpu": "2", "memory": "4Gi"}}})


def test_ingress_extension_is_explicit_and_requires_sandbox_id_linking() -> None:
    assert config().network_settings().ingress == "deployment"
    data = config_data()
    data["opensandbox"]["network"]["ingress"] = "sandbox"
    with pytest.raises(ValueError, match="requires link = sandbox"):
        FlotillaConfig.from_mapping(data)
    data["opensandbox"]["network"]["link"] = "sandbox"
    cfg = FlotillaConfig.from_mapping(data)
    assert cfg.network_settings().ingress == "sandbox"
    assert cfg.platform_capabilities().sandbox_ingress
    assert not FlotillaConfig.from_mapping(config_data()).platform_capabilities().sandbox_ingress


def test_the_filesystem_protocol_decides_the_filesystem_capabilities() -> None:
    """Read-only rootfs and tmpfs ride on a lifecycle extension, so the upstream dialect lacks them."""
    upstream = config().platform_capabilities()
    assert not (upstream.readonly_rootfs or upstream.native_tmpfs or upstream.shm_size)
    internal = config(
        opensandbox={**config_data()["opensandbox"], "filesystem_protocol": "sandbox-system-v1"}
    ).platform_capabilities()
    assert internal.readonly_rootfs and internal.native_tmpfs and internal.shm_size
    assert internal.filesystem_limits.max_tmpfs_mounts == 8


def test_management_settings_roundtrip_without_touching_task_capabilities(tmp_path: Path) -> None:
    probe = {"image": "reg/probe@sha256:" + "1" * 64}
    cfg = config(
        opensandbox={**config_data()["opensandbox"], "management_extensions": {"runtimeClassName": "kata"}},
        prepare={"management_profile": "management", "management_resources": {"cpu": "2", "memory": "4Gi"}},
        probe=probe,
    )
    assert cfg.probe_settings() == config(probe=probe).probe_settings()
    assert cfg.platform_capabilities() == config().platform_capabilities()
    restored = FlotillaConfig.from_mapping(cfg.model_dump(mode="json"))
    assert restored.prepare == cfg.prepare
    assert restored.opensandbox_settings().management_extensions == {"runtimeClassName": "kata"}
    assert restored.anchor_settings(workspace=True) == cfg.anchor_settings(workspace=True)


@pytest.mark.parametrize(
    "prepare",
    [
        {"management_profile": "kata"},  # Only a closed internal marker, not a free-form runtime name.
        {"management_profile": "management"},  # No backend-specific management settings.
        {"management_resources": {"cpu": "0", "memory": "4Gi"}},
        {"management_resources": {"cpu": "2", "memory": "0"}},
        {"management_resources": {"cpu": "2", "memory": "4Gi", "privileged": True}},
    ],
)
def test_invalid_management_configuration_is_rejected(prepare: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="management"):
        config(prepare=prepare)


def test_bootstrap_grants_no_capabilities() -> None:
    cfg = config()
    caps = cfg.management_capabilities()
    assert not caps.link and not caps.privileged_runtime and not caps.shared_volume
    assert not caps.external_forms and not caps.readonly_rootfs


def test_probe_inputs_are_config_not_capability_declarations() -> None:
    cfg = config(probe={"image": "reg/probe@sha256:" + "1" * 64, "link_members": 9})
    assert cfg.probe_settings().image == cfg.probe.image
    with pytest.raises(ValidationError):
        config(probe={"image": "reg/probe:latest"})
    with pytest.raises(ValidationError):
        config(probe={"link_members": 100})
    with pytest.raises(ValidationError):
        config(probe={"link": True})


@pytest.mark.parametrize(
    ("service", "protocol", "port"),
    [("echo", "tcp", 48080), ("echo", "udp", 48080), ("https", "tcp", 443), ("ntp", "udp", 123)],
)
def test_probe_service_config_roundtrip(
    service: Literal["echo", "https", "ntp"], protocol: Literal["tcp", "udp"], port: int
) -> None:
    from flotilla.capabilities import ExternalTarget

    target = {"host": "control.example.com", "port": port, "protocol": protocol, "service": service}
    cfg = config(probe={"image": "reg/probe@sha256:" + "1" * 64, "external_targets": [target]})
    restored = FlotillaConfig.from_mapping(cfg.model_dump(mode="json"))
    assert restored.probe == cfg.probe
    assert restored.probe_settings().targets == (ExternalTarget("control.example.com", port, protocol, service),)


@pytest.mark.parametrize("service,protocol", [("https", "udp"), ("ntp", "tcp"), ("http", "tcp")])
def test_invalid_probe_service_config_is_rejected(service: str, protocol: str) -> None:
    with pytest.raises(ValidationError):
        config(
            probe={
                "external_targets": [
                    {"host": "control.example.com", "port": 443, "protocol": protocol, "service": service}
                ]
            }
        )


def test_legacy_probe_target_defaults_to_echo() -> None:
    cfg = config(
        probe={"image": "reg/probe@sha256:" + "1" * 64, "external_targets": [{"host": "192.0.2.1", "port": 48080}]}
    )
    assert cfg.probe_settings().targets[0].service == "echo"


def test_datasets_default_to_the_work_root(tmp_path: Path) -> None:
    cfg = config(inputs={}, datasets={"tb": {"local_root": str(tmp_path), "platform_root": "datasets/tb"}})
    assert cfg.inputs == {} and cfg.datasets["tb"].input is None
    assert cfg.storage_settings_for("work") == cfg.storage_settings()
    assert cfg.anchor_settings().inputs == ()


def test_named_inputs_bind_datasets_and_the_anchor(tmp_path: Path) -> None:
    cfg = config(
        storage={"host_path": "@user", "root_subpath": "work"},
        inputs={
            "datasets": {"host_path": "@user:owner", "root_subpath": "datasets"},
            "runtimes": {"host_path": "@user:owner", "root_subpath": "runtimes"},
        },
        datasets={
            "tb": {"local_root": str(tmp_path), "platform_root": "tb/r1", "input": "datasets"},
            "local": {"local_root": str(tmp_path), "platform_root": "sets/r2"},
        },
    )
    assert cfg.storage_settings().root == "host:@user/work"
    assert cfg.storage_settings_for("datasets").root == "host:@user:owner/datasets"
    assert set(cfg.input_settings()) == {"datasets", "runtimes"}
    # Each dataset resolves against the storage root it was declared on, the work root by default.
    resolved = cfg.resolve_datasets()
    assert {name: ds.root for name, ds in resolved.items()} == {"tb": "datasets", "local": "work"}
    assert cfg.anchor_settings().inputs == ("datasets", "runtimes")
    assert cfg.anchor_settings(workspace=True).inputs == ("datasets", "runtimes")


@pytest.mark.parametrize(
    "update,match",
    [
        ({"inputs": {"work": {"host_path": "@user"}}}, "work"),
        ({"inputs": {"Bad": {"host_path": "@user"}}}, "name"),
        ({"datasets": {"tb": {"local_root": "/r", "platform_root": "tb/r1", "input": "missing"}}}, "missing"),
        (
            {
                "agent_runtime": {
                    "input": "missing",
                    "layers": {"lagent": {"local_root": "/r", "platform_root": "runtimes/lagent"}},
                }
            },
            "missing",
        ),
        ({"agent_runtime": {"layers": {"Bad": {"local_root": "/r", "platform_root": "x"}}}}, "name"),
    ],
)
def test_input_names_are_checked(update: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        config(**update)


def test_voyager_section_is_rejected_with_a_migration_error() -> None:
    with pytest.raises(ValidationError, match=r"\[agent_runtime\.layers"):
        config(voyager={"agent": "claude_code", "runtime": {}})


def test_agent_runtime_is_typed_and_layer_agnostic(tmp_path: Path) -> None:
    assert config().agent_runtime is None
    data = config(
        inputs={"runtimes": {"host_path": "@user:owner", "root_subpath": "runtimes"}},
        agent_runtime={
            "input": "runtimes",
            "layers": {
                "lagent": {"local_root": str(tmp_path / "layers/lagent"), "platform_root": "runtimes/lagent"},
                "anything": {"local_root": "/abs/layer", "platform_root": "runtimes/anything"},
            },
        },
    ).model_dump(mode="json")
    cfg = FlotillaConfig.from_mapping(data)
    assert cfg.agent_runtime is not None
    # Flotilla validates shape and roots; layer names stay consumer vocabulary.
    assert set(cfg.agent_runtime.layers) == {"lagent", "anything"}
    assert cfg.agent_runtime.layers["lagent"].local_root == tmp_path / "layers/lagent"
    assert FlotillaConfig.from_mapping(cfg.model_dump(mode="json")) == cfg


def test_a_client_side_path_must_be_absolute() -> None:
    """A spec outlives the directory it was written in, so a relative path has no anchor left."""
    with pytest.raises(ValidationError, match=r"datasets\.tb"):
        config(datasets={"tb": {"local_root": "data/tb", "platform_root": "datasets/tb"}})
    with pytest.raises(ValidationError, match=r"agent_runtime\.layers\.node"):
        config(
            inputs={"runtimes": {"host_path": "@user:owner", "root_subpath": "runtimes"}},
            agent_runtime={"input": "runtimes", "layers": {"node": {"platform_root": "n", "local_root": "layers/n"}}},
        )


def test_share_defaults_to_the_builtin_release() -> None:
    data = config_data()
    del data["share"]
    assert FlotillaConfig.from_mapping(data).share.builtin
    assert not config().share.builtin  # an explicit custom pin stays custom


def test_runtime_layer_local_root_is_optional() -> None:
    cfg = config(
        inputs={"runtimes": {"host_path": "@user:owner", "root_subpath": "runtimes"}},
        agent_runtime={"input": "runtimes", "layers": {"node": {"platform_root": "node/node22"}}},
    )
    assert cfg.agent_runtime is not None and cfg.agent_runtime.layers["node"].local_root is None
    assert FlotillaConfig.from_mapping(cfg.model_dump(mode="json")).agent_runtime == cfg.agent_runtime
