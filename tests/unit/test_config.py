"""`FlotillaConfig`: load validation, capability report checks, core/backend translation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from flotilla.config import ConfigError, FlotillaConfig
from flotilla.platform.base import ExternalPolicy
from flotilla.testing.config import SHA, config, config_data
from flotilla.testing.reports import no, report

# ───────────────────────────── Loading ─────────────────────────────


def test_loads_toml_and_resolves_report_path(tmp_path: Path) -> None:
    (tmp_path / "flotilla.toml").write_text(
        f"""
[opensandbox]
endpoint = "http://os.test"
credential_env = "FLOTILLA_CRED"
capabilities = "deployments/test.json"

[opensandbox.network]
sandbox_cidrs = ["10.0.0.0/16"]

[opensandbox.create_fields]
ports = [{{ containerPort = 44772, name = "execd", protocol = "tcp" }}]

[storage]
volumes = "host"
host_path = "/srv/flotilla"

[share]
release = "2026-10-07.1"
release_sha256 = "{SHA}"
"""
    )
    cfg = FlotillaConfig.load(tmp_path / "flotilla.toml")
    assert cfg.capabilities_path() == tmp_path / "deployments/test.json"
    assert cfg.opensandbox_settings().create_fields["ports"][0]["containerPort"] == 44772
    assert cfg.storage_settings().host_path == "/srv/flotilla"
    assert cfg.network.policy() == ExternalPolicy(mode="none")  # no external given: only none


def test_build_publication_settings() -> None:
    defaults = config().build.settings()
    assert defaults.oci_mediatypes is False
    assert defaults.attestations is False
    explicit = config(build={"oci_mediatypes": True, "attestations": True}).build.settings()
    assert explicit.oci_mediatypes is True
    assert explicit.attestations is True
    with pytest.raises(ValueError, match="oci_mediatypes"):
        config(build={"attestations": True}).build.settings()


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


# ───────────────────────────── Checks against the capability report ─────────────────────────────


def test_matching_report_passes() -> None:
    config().check_against(report())


def test_collects_all_problems() -> None:
    bad = report(
        **{
            "lifecycle.label_list": no(),
            "runtime.exec_auth": no(),
            "network.external_allowlist": no(),
        }
    )
    with pytest.raises(ConfigError) as exc:
        config().check_against(bad)
    problems = exc.value.problems
    assert any("lifecycle list by label" in p for p in problems)
    assert any("exec_auth" in p and "accept_insecure" in p for p in problems)
    assert any("allowlist" in p for p in problems)  # a narrow allowlist needs deployment allowlist support


def test_endpoint_mismatch_rejected() -> None:
    with pytest.raises(ConfigError, match="endpoint"):
        config().check_against(report(endpoint="http://other.test"))


def test_security_gaps_require_explicit_acceptance_and_no_stale_entries() -> None:
    gap = report(**{"storage.no_auto_mount": no()})
    config(security={"accept_insecure": ["no_auto_mount"]}).check_against(gap)
    with pytest.raises(ConfigError, match="no_auto_mount"):
        config().check_against(gap)
    config(security={"accept_insecure": ["exec_auth"]}).check_against(report())


def test_gc_window_must_exceed_grace_plus_visibility() -> None:
    cfg = config(probe={"list_visibility_s": 120.0})
    slow = report(evidence_profile=cfg.evidence_profile())
    with pytest.raises(ConfigError, match="gc_window"):
        cfg.check_against(slow)
    config(probe={"list_visibility_s": 120.0}, reaper={**config_data()["reaper"], "gc_window": 300}).check_against(slow)


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
    cfg.check_against(report(evidence_profile=cfg.evidence_profile()))
    assert cfg.runtime_params["UPSTREAM"] == value


def test_any_rejects_runtime_param_on_platform_infrastructure() -> None:
    cfg = config(network={"external": "any"}, runtime_params={"META": "http://169.254.169.254/latest"})
    cfg.check_against(report(evidence_profile=cfg.evidence_profile()))


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
    cfg.check_against(report(evidence_profile=cfg.evidence_profile()))


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
    evidence = report(**{"network.inbound_isolation": {"status": "failed"}, "evidence_profile": cfg.evidence_profile()})
    assert cfg.network_settings().ingress == "sandbox"
    caps = cfg.platform_capabilities(evidence)
    assert not caps.supports_network_none and not caps.inbound_isolation
    assert not evidence.network.inbound_isolation.ok  # opting in does not fabricate probe evidence


@pytest.mark.parametrize(
    "section,value",
    [
        ("storage", {"host_path": "/other"}),
        ("share", {"release": "other", "release_sha256": SHA}),
        ("network", {"external": []}),
        ("inputs", {"datasets": {"host_path": "@user:owner"}}),
    ],
)
def test_relevant_config_changes_reject_stale_evidence(section: str, value: dict[str, Any]) -> None:
    cfg = config(**{section: value})
    with pytest.raises(ConfigError, match="evidence"):
        cfg.platform_capabilities(report())


def test_protocol_runtime_extension_binding() -> None:
    for changes in (
        {"execd": {"protocol": "legacy"}},
        {"extensions": {"project": "other"}},
        {"privileged_extensions": {"runtimeClassName": "kata"}},
        {"filesystem_protocol": "sandbox-system-v1"},
    ):
        cfg = config(opensandbox={**config_data()["opensandbox"], **changes})
        assert cfg.evidence_profile() != config().evidence_profile()


def test_build_prepare_changes_do_not_invalidate_evidence() -> None:
    cfg = config(prepare={"concurrency": 17}, build={"image_replacements": {"a": "b"}})
    assert cfg.evidence_profile() == config().evidence_profile()
    assert cfg.platform_capabilities(report()).link


def test_management_settings_roundtrip_without_changing_task_evidence(tmp_path: Path) -> None:
    probe = {"image": "reg/probe@sha256:" + "1" * 64}
    cfg = config(
        opensandbox={**config_data()["opensandbox"], "management_extensions": {"runtimeClassName": "kata"}},
        prepare={"management_profile": "management", "management_resources": {"cpu": "2", "memory": "4Gi"}},
        probe=probe,
    )
    assert cfg.evidence_profile() == config().evidence_profile()
    assert cfg.filesystem_profile() == config().filesystem_profile()
    assert cfg.probe_settings() == config(probe=probe).probe_settings()
    assert cfg.platform_capabilities(report()) == config().platform_capabilities(report())
    restored = FlotillaConfig.from_mapping(cfg.to_mapping(), base_dir=tmp_path)
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


def test_bootstrap_needs_no_report_and_grants_no_capabilities() -> None:
    cfg = config()
    caps = cfg.management_capabilities()
    assert not caps.link and not caps.privileged_runtime and not caps.shared_volume
    assert not caps.external_forms and not caps.readonly_rootfs


def test_probe_inputs_are_config_not_capability_declarations() -> None:
    cfg = config(probe={"image": "reg/probe@sha256:" + "1" * 64, "link_members": 9})
    assert cfg.probe_settings().image == cfg.probe.image
    assert cfg.probe_settings().evidence_profile == cfg.evidence_profile()
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
    service: Literal["echo", "https", "ntp"], protocol: Literal["tcp", "udp"], port: int, tmp_path: Path
) -> None:
    from flotilla.capabilities import ExternalTarget

    target = {"host": "control.example.com", "port": port, "protocol": protocol, "service": service}
    cfg = config(probe={"image": "reg/probe@sha256:" + "1" * 64, "external_targets": [target]})
    restored = FlotillaConfig.from_mapping(cfg.to_mapping(), base_dir=tmp_path)
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


def test_serialized_config_keeps_evidence_binding(tmp_path: Path) -> None:
    cfg = config()
    restored = FlotillaConfig.from_mapping(cfg.to_mapping(), base_dir=tmp_path)
    assert restored.evidence_profile() == cfg.evidence_profile()


def test_releases_default_to_the_work_root(tmp_path: Path) -> None:
    cfg = config(task_releases={"tb": {"local_root": str(tmp_path), "platform_root": "releases/tb"}})
    assert cfg.inputs == {} and cfg.task_releases["tb"].input is None
    assert cfg.storage_settings_for("work") == cfg.storage_settings()
    assert cfg.anchor_settings().inputs == ()
    assert "inputs" not in cfg.evidence_profile()


def test_named_inputs_bind_releases_and_the_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import flotilla.native

    cfg = config(
        storage={"host_path": "@user", "root_subpath": "work"},
        inputs={
            "datasets": {"host_path": "@user:owner", "root_subpath": "datasets"},
            "runtimes": {"host_path": "@user:owner", "root_subpath": "runtimes"},
        },
        task_releases={
            "tb": {"local_root": str(tmp_path), "platform_root": "tb/r1", "input": "datasets"},
            "local": {"local_root": str(tmp_path), "platform_root": "releases/r2"},
        },
    )
    assert cfg.storage_settings().root == "host:@user/work"
    assert cfg.storage_settings_for("datasets").root == "host:@user:owner/datasets"
    assert set(cfg.input_settings()) == {"datasets", "runtimes"}
    seen: list[Any] = []

    def capture(bindings, *, storage, inputs):
        seen.append(({name: b.root for name, b in bindings.items()}, storage, inputs))

    monkeypatch.setattr(flotilla.native, "ReleaseBindings", capture)
    cfg.release_bindings()
    assert seen == [({"tb": "datasets", "local": "work"}, cfg.storage_settings(), cfg.input_settings())]
    assert cfg.anchor_settings().inputs == ("datasets", "runtimes")
    assert cfg.anchor_settings(workspace=True).inputs == ("datasets", "runtimes")


@pytest.mark.parametrize(
    "update,match",
    [
        ({"inputs": {"work": {"host_path": "@user"}}}, "work"),
        ({"inputs": {"Bad": {"host_path": "@user"}}}, "name"),
        ({"task_releases": {"tb": {"local_root": "/r", "platform_root": "tb/r1", "input": "missing"}}}, "missing"),
    ],
)
def test_input_names_are_checked(update: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        config(**update)
