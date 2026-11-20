# Writing a backend

A backend connects Flotilla to one sandbox platform. Everything above it is platform-independent: Compose parsing, task resolution, the anchor, file publication, network-policy planning, health gates, renewal, cleanup, and GC all go through the `Platform` protocol in [`flotilla/platform/base.py`](../src/flotilla/platform/base.py). The built-in backend is OpenSandbox ([`flotilla/platform/opensandbox/`](../src/flotilla/platform/opensandbox/)), registered in the `BACKENDS` table in `flotilla/config.py`; adding a key to that table is the whole registration mechanism.

Read [Architecture](Architecture.md) first for the terminology (anchor, share release, trial, launch).

## What the platform must offer

Before writing code, check that the platform can do the following. Flotilla does not emulate any of them.

| Need | Used for |
|---|---|
| Create, get, list-by-label, delete, and renew (extend the expiry of) single-container instances | Ownership, reconciliation after lost create responses, GC |
| Run a command with an explicit argv, uid/gid, cwd, and a complete environment; stream or return its output and exit code | All execution, health checks, anchor file operations |
| Run a command in the background, query its status, and interrupt that exact command | Background processes, confirmed timeouts and cancellation |
| Read and write a file with numeric owner and mode | `/etc/hosts`, file transfer |
| Mount subdirectories of one shared storage root, read-only or read-write | Share release at `/.flotilla`, task files, shared volumes, the anchor |
| A per-instance egress policy: none, an allowlist, or any | Network isolation |
| A way for instances of one environment to reach each other by address | Multi-service tasks (optional: single-service tasks work without it) |

## Layout

A backend is one module under `flotilla/platform/` plus its settings and registration:

```text
src/flotilla/platform/mycloud/
  backend.py      # settings model + backend class, added to BACKENDS
  ...             # Platform implementation
```

Register the class in the in-tree table in `flotilla/config.py`. The key is the value users put in `backend = "..."` and must match `[a-z][a-z0-9_-]*`:

```python
BACKENDS: dict[str, Backend[Any]] = {
    BUILTIN: OpenSandboxBackend(),
    "mycloud": MyCloudBackend(),
}
```

## Configuration

A deployment selects the backend by name and gives it a table under `backends`:

```python
backend="mycloud",
backends={
    "mycloud": {
        "endpoint": "https://api.mycloud.example",
        "credential_env": "MYCLOUD_TOKEN",
        "region": "cn-sh",
    }
},
storage=Storage(volumes="host", host_path="/mnt/shared/flotilla"),
# share, reaper, network, limits, ... are the same for every backend.
```

The table is validated by your settings model, which subclasses `BackendSettings`. The base class provides the two fields every backend needs and rejects unknown keys:

| Field | Meaning |
|---|---|
| `endpoint` | `http(s)` URL identifying the deployment |
| `credential_env` | Name of the environment variable holding the credential. Flotilla reads it and passes the value to `build` |

```python
from flotilla.backends import BackendSettings


class MyCloudSettings(BackendSettings):
    region: str
    instance_class: str = "standard"
```

Validation errors are reported with the field path (`backends.mycloud.region: Field required`) and never echo input values. Keep it that way: a pasted credential in the wrong key must not reach logs.

`[storage]` is shared by all backends. `cfg.storage_settings()` (and `cfg.input_settings()` for the named read-only input roots) returns its `volumes` (`"host"` or `"pvc"`), `host_path`, `claim_name`, and `root_subpath`. Map them to whatever your platform calls a shared mount. If your platform has a different storage model, add your own keys to the settings model and document how they relate to `[storage]`.

## The backend class

A backend satisfies the `Backend` protocol in [`flotilla/backends.py`](../src/flotilla/backends.py); there is no base class to inherit:

```python
from collections.abc import Awaitable, Callable

from flotilla.config import FlotillaConfig
from flotilla.platform.base import Capabilities, Clock, Platform

from flotilla.platform.mycloud.platform import MyCloudPlatform


class MyCloudBackend:
    settings_model = MyCloudSettings

    def check(self, settings: MyCloudSettings, cfg: FlotillaConfig) -> list[str]:
        if cfg.network.external == "any":
            return ["mycloud cannot express network.external = any; use an allowlist"]
        return []

    def build(
        self,
        settings: MyCloudSettings,
        cfg: FlotillaConfig,
        *,
        clock: Clock,
        credential: str,
        caps: Capabilities,
        command_gate: Callable[[], Awaitable[None]] | None = None,
    ) -> Platform:
        return MyCloudPlatform(
            endpoint=settings.endpoint,
            token=credential,
            region=settings.region,
            storage=cfg.storage_settings(),
            caps=caps,
            clock=clock,
            command_gate=command_gate,
            create_timeout_s=cfg.timeouts.create_request,
        )
```

| Hook | Purpose |
|---|---|
| `build` | Return a new `Platform`. The caller owns it and calls `aclose()`. Await `command_gate` (when given) before each user command request; it is the client's `limits.exec_rate` limiter |
| `check` | Cross-field checks between your table and the rest of the config, run at load time. Return messages; Flotilla raises them together |
| `capabilities` | Adjust the report's capabilities for what this configuration enforces or cannot provide; return them unchanged when nothing applies. OpenSandbox uses it to turn off read-only rootfs and tmpfs unless an extension protocol is selected |

`build` is called once per runtime client and once per CLI command (`probe`, `gc`, `share`). The same factory serves all of them, so a setting cannot apply to the runtime and silently not apply to the probe.

## The `Platform` contract

The core calls only these methods. Each docstring in `base.py` is the authoritative contract; the points below are the ones implementations most often get wrong.

**Error classification.** Raise `FlotillaError(message, stage=..., category=..., retryable=...)` for every platform failure. The category drives core decisions:

| Category | Meaning |
|---|---|
| `TRANSIENT` | The platform may or may not have acted (5xx, dropped connection, timeout). For `create`, only this category triggers reconciliation by labels |
| `RATE_LIMITED`, `CAPACITY` | Rejected before acting; may be retried later |
| `IMAGE` | The image cannot be pulled or run |
| `INVALID`, `NOT_FOUND` | Definite rejection |

Never include raw response bodies, credentials, environment values, or command output in messages.

**Lifecycle.**

- `create(spec)` is not idempotent and is never retried by the core. Apply `spec.labels` as platform labels or metadata; `list(labels)` must find the instance by them, because a lost create response is recovered that way. Use `spec.timeout_seconds` as the instance expiry. `spec.group`, when set, asks for co-placement of one environment's instances; ignore it if the platform does not need it.
- `InstanceHandle` holds only the instance ID. Anything else you need (an exec address, a node name) must be derivable from the ID, because the core rebuilds handles from `list` results.
- `get` and `list` return `InstanceState` with a normalized `InstanceStatus`: `PENDING` (cannot run commands yet), `RUNNING` (can), `TERMINAL` (never returns to running), `UNKNOWN` (anything you cannot map; treated as unhealthy). Put the native state string in `reason`.
- `delete` of a missing instance succeeds. A rejected delete must raise; the core only releases capacity after deletion is confirmed.
- `list` pages internally and returns every match. The deployment declares the platform's worst-case delay before a new instance appears in listings as `[platform].list_visibility_s`; GC safety depends on it.

**Execution.**

- `exec` runs exactly `proc.argv` (no shell) as `proc.uid:proc.gid` in `proc.cwd` with exactly `proc.env`. Do not merge in the platform agent's environment. If the platform cannot express identity or a clean environment natively, wrap the command; OpenSandbox runs it through BusyBox from the share release at `/.flotilla/bin/busybox`.
- When `proc.timeout_s` passes, stop the process and confirm it stopped before returning `ExecResult(timed_out=True)`. If you cannot confirm, raise `ExecOutcomeUnknown`. Never retry a command that may have been sent.
- Record progress in `attempt` when given: set `attempt.sent = True` once the request may have reached the platform, and `attempt.command_id` once the platform assigns an ID. Cancellation uses these to decide whether it must interrupt and confirm.
- An explicit "busy, not accepted" rejection from the exec channel is `CommandBusy`. Use it only when the platform guarantees the command did not start.
- `start_process` returns a process ID that `process_status` and `interrupt` accept. `process_status` returns `found=False` when the platform has no record, which the core treats as lost.

**Files.** `write_file` creates the file with the given numeric `uid`, `gid`, and `mode`, and `read_file` returns raw bytes. Both are binary-safe.

**Storage.** `spec.volumes` is a tuple of `SharedVolume(key, mount_path, read_only, root)`. `key` is a path relative to the shared root (`""` means the root itself, used only by the anchor). `root` is `"work"` (`[storage]`) or the name of an `[inputs.<name>]` root; input volumes are always read-only. Compile each volume against its root's settings and reject unknown names. Mount exactly these and nothing else: anything extra is a `no_auto_mount` security gap.

**Network.**

- `spec.external` is the instance's egress policy toward everything outside its environment: `none`, `allowlist` (hosts and CIDRs), or `any`. Apply it at creation.
- `internal_address(handle)` returns an address other instances of the same environment can reach. Flotilla writes it into each service's `/etc/hosts` under the Compose service names.
- `link(members, topology)` lets the named members reach each other according to `topology.networks` (network name to member names). It must be in effect when it returns and must be idempotent. Instances on no shared network must stay unreachable from each other.

**Other.** `caps` is the `Capabilities` passed to `build`. `forget_creates` drops local bookkeeping for unresolved creates and has no remote effect. `aclose` closes your HTTP clients; the runtime and each CLI command call it when they are done with the platform.

## Capabilities and the probe

`Backend.capabilities` is where your backend narrows what a deployment may be asked for. It receives everything on, because a platform that cannot do something refuses the request, and takes away what your configuration rules out. It must never add a capability back.

`flotilla probe` measures isolation through your `Platform` and reports it through its output and exit status; it writes nothing. A check your backend cannot express must raise `NotImplementedError`, which the probe records as not-shown. Never fabricate a success. See the [deployment guide](Deployment.md#isolation-measurement).

The probe follows the normal [deployment guide](Deployment.md): build and push the anchor and probe images, publish a share release, run `flotilla probe`, and read its output and exit status. Common checks use the Platform contract. Current independent ingress, execd authentication and privileged-runtime checks also use explicit OpenSandbox helpers; another backend must provide equivalent measured controls before those capabilities can be enabled. Management bootstrap uses conservative capabilities and must not bypass normal create gates.

## Testing

1. **Unit tests without a platform.** Test request mapping and error classification with a mock transport, as the OpenSandbox tests do with `httpx.MockTransport` ([`tests/unit/test_opensandbox_platform.py`](../tests/unit/test_opensandbox_platform.py)). Cover lost create responses, timeouts during exec, unconfirmable interrupts, and rejected deletes.
2. **Configuration.** Load a config with `backend = "<name>"` and check that table errors carry field paths and that `check` problems are reported. [`tests/unit/test_backends.py`](../tests/unit/test_backends.py) shows how to register a backend in a test the same way the real table is built.
3. **Reference behavior.** `flotilla.testing.platform.FakePlatform` is an in-memory `Platform` used by Flotilla's own runtime tests. Compare its behavior to yours when a contract is unclear.
4. **A real deployment.** Run `flotilla probe`, then open a single-service and a multi-service task with the [library quick start](Quick_Start.md), and confirm that `cleanup_report.clean` is true and that an independent listing of the platform shows no labeled instances afterwards.

The opt-in real-platform contracts in `tests/contract/` currently construct OpenSandbox clients directly and do not yet run against third-party backends.

## Checklist

- [ ] Registered in `BACKENDS`, settings model rejects unknown keys, credential read only through `credential_env`.
- [ ] Every platform failure raised as `FlotillaError` with a deliberate category; create ambiguity reported as `TRANSIENT`.
- [ ] Labels are filterable by `list`, and the deployment's declared `[platform].list_visibility_s` holds.
- [ ] `exec` uses exact argv, identity, cwd, and environment; timeouts are confirmed stopped; `attempt` is updated.
- [ ] Only the requested volumes are mounted.
- [ ] Egress policies and links are enforced, with positive and negative checks ([isolation measurement](Deployment.md#isolation-measurement)).
- [ ] `flotilla probe` passes the required items, and a real run ends with a clean cleanup report.
