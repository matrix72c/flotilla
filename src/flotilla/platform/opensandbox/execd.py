"""Explicit current/legacy execution and file protocols; commands are never replayed."""

from __future__ import annotations

import asyncio
import json
import math
import posixpath
import re
import secrets
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal
from urllib.parse import quote

from flotilla.platform.base import (
    CommandBusy,
    ErrorCategory,
    ExecAttempt,
    ExecOutcomeUnknown,
    ExecResult,
    FlotillaError,
    ProcessSpec,
    ProcessStatus,
    Stage,
)
from flotilla.platform.execution import confirm_stopped
from flotilla.platform.opensandbox.http import Http

BUSYBOX = "/.flotilla/bin/busybox"  # static busybox on the share
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # variable names referenceable as "$K" in sh
_CHOWN_TIMEOUT_S = 30.0  # chown after legacy upload
#: The wrapper path is written verbatim into `sh -c` scripts: only characters needing no quoting, so spaces, `$`,
#: or quotes in the path are never interpreted by the shell.
_SAFE_PATH = re.compile(r"/[A-Za-z0-9_./-]+")
#: Under legacy, `envs` first enter execd's outer (non-interactive) bash, where these change the shell itself --
#: `BASH_ENV` is sourced, `SHELLOPTS=noexec` makes every command silently return 0 (false health check passes),
#: likewise `BASHOPTS` / `ENV`. Docker has no such layer; reject them.
LEGACY_UNSAFE_ENV = frozenset({"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"})

ExecdProtocol = Literal["current", "legacy"]


@dataclass(frozen=True)
class ExecdSettings:
    """Deployment config `[opensandbox.execd]`."""

    protocol: ExecdProtocol = "current"
    #: Same path on all instances; needs applets sh, env, id, awk (wrap), ip (address), chown, chmod (legacy upload).
    wrapper: str = BUSYBOX
    output_limit_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.protocol not in ("current", "legacy"):
            raise ValueError(f"execd.protocol must be current or legacy: {self.protocol!r}")
        if not _SAFE_PATH.fullmatch(self.wrapper) or posixpath.normpath(self.wrapper) != self.wrapper:
            raise ValueError(f"execd.wrapper must be a normalized absolute path in [A-Za-z0-9_./-]: {self.wrapper!r}")


# Pure functions: SSE (NDJSON) parsing, exit code normalization, argv wrapping, multipart encoding


def parse_events(text: str) -> list[dict[str, Any]]:
    """Split execd's NDJSON stream into event objects. Each event is one bare JSON line, separated by blank lines.

    Non-JSON lines are rejected as an unknown outcome; SSE comments starting with `:` and `data:` prefixes are allowed.
    """
    events: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
            if not line:
                continue
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise ExecOutcomeUnknown("execd returned malformed NDJSON") from exc
        if not isinstance(obj, dict):
            raise ExecOutcomeUnknown("execd event is not an object")
        events.append(obj)
    return events


def fold_exec(events: Sequence[Mapping[str, Any]]) -> ExecResult:
    """Accumulate stdout/stderr and normalize the exit code and timeout.

    Output is rebuilt by line: execd sends one event per line with the line ending stripped and `"\n"` for blank
    lines, so `\n` is appended to other events. This is lossy -- `\r` becomes `\n` and an unterminated last line
    gains `\n`; line-based callers (directory listing, log tails, address parsing) are unaffected, but byte-exact
    output must be written to a file and fetched with `read_file`.

    `error.evalue` (or legacy flat `evalue`) must be an integer; success requires `execution_complete`.
    A missing or invalid terminal event raises ExecOutcomeUnknown with partial output. A negative exit code only
    means signal termination and cannot alone confirm a timeout; the runtime sets timed_out from the local
    deadline plus confirmed remote termination.
    """
    stdout: list[str] = []
    stderr: list[str] = []
    exit_code: int | None = None
    completed = False
    command_id = None
    invalid_error = False
    for ev in events:
        kind = ev.get("type")
        if kind == "init":
            command_id = ev.get("text")
        elif kind == "stdout":
            stdout.append(_line(ev))
        elif kind == "stderr":
            stderr.append(_line(ev))
        elif kind == "execution_complete":
            completed = True
        elif kind == "error":
            exit_code = _error_exit_code(ev)
            invalid_error = exit_code is None
    if invalid_error or (exit_code is None and not completed):
        raise ExecOutcomeUnknown(
            "execd stream has no valid terminal result",
            stdout="".join(stdout).encode(),
            stderr="".join(stderr).encode(),
            command_id=command_id,
        )
    if exit_code is None:
        exit_code = 0
    return ExecResult(
        exit_code=exit_code,
        stdout="".join(stdout).encode(),
        stderr="".join(stderr).encode(),
        timed_out=False,
    )


def _line(event: Mapping[str, Any]) -> str:
    text = str(event.get("text") or "")
    return text if text == "\n" else text + "\n"


def _error_exit_code(event: Mapping[str, Any]) -> int | None:
    """Exit code from an `error` event: nested `error.evalue`, else flat `evalue`; non-integer means unknown outcome."""
    err = event.get("error")
    value = err.get("evalue") if isinstance(err, dict) else event.get("evalue")
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def check_env_keys(env: Mapping[str, str]) -> None:
    """Names must be referenceable as `"$K"` in sh (`[A-Za-z_][A-Za-z0-9_]*`): the wrapper only writes names."""
    bad = sorted(key for key in env if not _ENV_KEY.fullmatch(key))
    if bad:
        raise FlotillaError(
            f"invalid environment variable names (must match [A-Za-z_][A-Za-z0-9_]*): {bad!r}",
            stage="run",
            category=ErrorCategory.INVALID,
            retryable=False,
        )


def wrap_argv(argv: Sequence[str], env_keys: Sequence[str], *, wrapper: str) -> list[str]:
    """Wrap with `env -i` to clear the inherited env, bring back only `env_keys` by name, then exec the argv.

    Values reach sh's env via the request `envs`; sh expands `K="$K"` for `env -i`; `$0` is `flotilla`, `"$@"` the
    original argv. The script holds only validated names (`check_env_keys`), never values. Keys are sorted
    (deterministic). `wrapper` is the busybox path.
    """
    assigns = "".join(f'{key}="${key}" ' for key in sorted(env_keys))
    command = f'exec {wrapper} env -i {assigns}"$@"'
    if "HOME" not in env_keys:
        # Resolve inside the cleared environment, after execd applies uid/gid.
        # Never reuse the daemon's HOME or load a login shell. Read passwd on
        # each execution so user changes within a running sandbox take effect.
        defaults = (
            'HOME="$("$0" awk -F: -v uid="$("$0" id -u)" '
            "'$3 == uid {print $6; exit}' /etc/passwd 2>/dev/null)\"; "
            f'exec "$0" env -i HOME="${{HOME:-/}}" {assigns}"$@"'
        )
        command = f'exec {wrapper} env -i {assigns}{wrapper} sh -c {shlex.quote(defaults)} {wrapper} "$@"'
    return [wrapper, "sh", "-c", command, "flotilla", *argv]


def multipart_upload(path: str, data: bytes, mode: int) -> tuple[str, bytes]:
    """Build the full multipart body for execd's two-part upload, returning (content_type, body).

    `mode` is sent as octal digits read as decimal (`0o644` -> `644`, matching execd's
    `strconv.ParseUint(itoa(mode), 8, 32)`). No owner/group: ownership comes from the identity route uid/gid. The
    body is built in memory so the caller can set `Content-Length` -- avoiding httpx's chunked streaming
    multipart (which fails through gateway proxies).
    """
    meta = json.dumps({"path": path, "mode": _octal_mode(mode)}).encode()
    dash, crlf = b"--", b"\r\n"
    boundary = secrets.token_hex(24).encode()
    while boundary in data or boundary in meta:
        boundary = secrets.token_hex(24).encode()
    body = b"".join(
        [
            dash,
            boundary,
            crlf,
            b'Content-Disposition: form-data; name="metadata"; filename="metadata"',
            crlf,
            b"Content-Type: application/json",
            crlf,
            crlf,
            meta,
            crlf,
            dash,
            boundary,
            crlf,
            b'Content-Disposition: form-data; name="file"; filename="file"',
            crlf,
            b"Content-Type: application/octet-stream",
            crlf,
            crlf,
            data,
            crlf,
            dash,
            boundary,
            dash,
            crlf,
        ]
    )
    return f"multipart/form-data; boundary={boundary.decode()}", body


def _octal_mode(mode: int) -> int:
    """`0o644` (decimal 420) -> `644`: the permission bits' octal digits as a decimal integer (like Go `OctalMode`)."""
    return int(format(mode & 0o7777, "o"))


def identity_prefix(uid: int, gid: int) -> str:
    return f"/v1/filesystem/{uid}/{gid}"


# Execd: the exec channel of one instance


class Execd:
    """Exec, background processes, and file ops on one instance's execd. `http` targets it (via the server proxy)."""

    #: Total deadline slack (s) for command requests: foreground uses `ProcessSpec.timeout_s` + this (execd needs
    #: time to kill the group and finish SSE), background start uses it alone. Read timeouts can't bound commands --
    #: execd pings every 3 seconds, so the gap never times out.
    HTTP_SLACK_S = 30.0

    def __init__(
        self, http: Http, settings: ExecdSettings, *, command_gate: Callable[[], Awaitable[None]] | None = None
    ) -> None:
        self._http = http
        self.settings = settings
        self._command_gate = command_gate

    async def exec(self, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        """Foreground exec: stream SSE, normalize exit code and timeout. Not retried (commands are not idempotent)."""
        if proc.timeout_s is None:
            raise ValueError("exec requires ProcessSpec.timeout_s (only a timeout can end a foreground command)")
        attempt = attempt if attempt is not None else ExecAttempt()
        # The remote deadline is a crash fallback. The local deadline determines
        # timeout attribution, so ordinary signal exits are never called timeouts.
        remote = replace(proc, timeout_s=proc.timeout_s + self.HTTP_SLACK_S)
        body = command_body(remote, background=False, settings=self.settings)
        work = asyncio.create_task(
            self._stream_command(body, stage="run", deadline_s=proc.timeout_s + 2 * self.HTTP_SLACK_S, attempt=attempt)
        )
        timer = asyncio.create_task(self._http.clock.sleep(proc.timeout_s))
        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                try:
                    return fold_exec(parse_events(work.result()))
                except FlotillaError as exc:
                    if attempt.sent and exc.retryable and not isinstance(exc, CommandBusy):
                        raise ExecOutcomeUnknown("command response was lost", command_id=attempt.command_id) from None
                    raise
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            if not attempt.sent:
                raise FlotillaError(
                    "command expired before sending", stage="run", category=ErrorCategory.CAPACITY, retryable=True
                )
            stopped = await confirm_stopped(
                self._http.clock, attempt.command_id, self.interrupt, self.process_status, self.HTTP_SLACK_S
            )
            if not stopped:
                raise ExecOutcomeUnknown("command deadline exceeded; stop unconfirmed", command_id=attempt.command_id)
            return ExecResult(-1, b"", b"", timed_out=True)
        finally:
            work.cancel()
            timer.cancel()
            await asyncio.gather(work, timer, return_exceptions=True)

    async def start_process(self, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> str:
        attempt = attempt if attempt is not None else ExecAttempt()
        body = command_body(proc, background=True, settings=self.settings)
        try:
            text = await self._stream_command(body, stage="start", deadline_s=self.HTTP_SLACK_S, attempt=attempt)
            for event in parse_events(text):
                if event.get("type") == "init" and event.get("text"):
                    attempt.command_id = str(event["text"])
                    return attempt.command_id
        except FlotillaError as error:
            if not attempt.sent or not error.retryable or isinstance(error, CommandBusy):
                raise
        raise ExecOutcomeUnknown("background start outcome is unknown", command_id=attempt.command_id)

    async def interrupt(self, pid: str) -> None:
        await self._http.request("DELETE", "/command", params={"id": pid}, stage="run", idempotent=False)

    async def process_status(self, pid: str) -> ProcessStatus:
        """`GET /command/status/{id}`; 404 (record lost) -> `found=False`, which the core treats as `lost`."""
        try:
            result = await self._http.request(
                "GET", f"/command/status/{quote(pid, safe='')}", stage="run", idempotent=True
            )
        except FlotillaError as exc:
            if exc.category is ErrorCategory.NOT_FOUND:
                return ProcessStatus(found=False, running=False, exit_code=None)
            raise
        if not isinstance(result, dict) or not isinstance(result.get("running"), bool):
            raise FlotillaError(
                "invalid command status response", stage="run", category=ErrorCategory.TRANSIENT, retryable=True
            )
        exit_code = result.get("exit_code")
        if exit_code is not None and type(exit_code) is not int:
            raise FlotillaError(
                "invalid command exit status", stage="run", category=ErrorCategory.TRANSIENT, retryable=True
            )
        return ProcessStatus(
            found=True,
            running=bool(result.get("running")),
            exit_code=int(exit_code) if exit_code is not None else None,
        )

    async def write_file(self, path: str, data: bytes, *, mode: int, uid: int, gid: int) -> None:
        """Fixed-length multipart upload, overwriting with `O_TRUNC`. Not retried.

        `current`: `/v1/filesystem/{uid}/{gid}/files/upload`, written as the target identity.
        `legacy`: `/files/upload` written by execd (root); for a non-root target, one more exec runs `chown` and
        resets mode (the kernel clears setuid / setgid when root changes ownership). Unlike `current`: written as
        root without checking the target's directory write permission; if upload succeeds but `chown` fails, the
        file exists owned by root and this is reported as `invalid`.
        """
        content_type, body = multipart_upload(path, data, mode)
        if self.settings.protocol == "legacy":
            # Legacy upload expands `$VAR` in paths via execd's env (`pathutil.ExpandPath`); chown takes it literally.
            if not posixpath.isabs(path) or "$" in path:
                raise ValueError(f"legacy execd write_file requires an absolute path without `$`: {path!r}")
            route = "/files/upload"
        else:
            route = f"{identity_prefix(uid, gid)}/files/upload"
        await self._http.request(
            "POST",
            route,
            stage="run",
            idempotent=False,
            content=body,
            headers={"Content-Type": content_type},
        )
        if self.settings.protocol == "legacy" and (uid, gid) != (0, 0):
            await self._chown(path, mode=mode, uid=uid, gid=gid)

    async def read_file(self, path: str) -> bytes:
        """`GET /files/download?path=...` for raw bytes; 404 -> `not_found`. Idempotent and retryable."""
        return await self._http.download("/files/download", stage="run", params={"path": path})

    async def _chown(self, path: str, *, mode: int, uid: int, gid: int) -> None:
        """Set owner and mode after legacy upload. Applets run via the wrapper's absolute path, not image PATH/tools."""
        w = self.settings.wrapper
        script = '"$0" chown "$1" "$3" && "$0" chmod "$2" "$3"'
        argv = (w, "sh", "-c", script, w, f"{uid}:{gid}", format(mode & 0o7777, "o"), path)
        proc = ProcessSpec(argv=argv, uid=0, gid=0, cwd="/", env={}, timeout_s=_CHOWN_TIMEOUT_S)
        result = await self.exec(proc)
        if result.timed_out or result.exit_code < 0:
            raise FlotillaError(
                f"{path} was written, but setting owner and mode timed out ({_CHOWN_TIMEOUT_S}s)",
                stage="run",
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            )
        if result.exit_code != 0:
            raise FlotillaError(
                f"{path} was written, but chown {uid}:{gid} / chmod {mode:o} failed (execd must run as root and the"
                f" wrapper must include both applets): {result.stderr.decode(errors='replace').strip()[-300:]}",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )

    async def _stream_command(
        self, body: Mapping[str, Any], *, stage: Stage, deadline_s: float, attempt: ExecAttempt | None = None
    ) -> str:
        """`POST /command`, reading the whole NDJSON stream as text. Not idempotent; not retried even on errors."""
        if self._command_gate is not None:
            await self._command_gate()
        return await self._http.stream_post(
            "/command",
            json=body,
            stage=stage,
            deadline_s=deadline_s,
            attempt=attempt,
            max_bytes=self.settings.output_limit_bytes,
        )


def command_body(proc: ProcessSpec, *, background: bool, settings: ExecdSettings) -> dict[str, Any]:
    """`RunCommandRequest`: the command holds only variable names, values go in `envs`; timeout is in ms.

    `current` sends the wrapped command as `argv`; `legacy` sends `command`: `shlex.join`, split once by execd's
    shell back into the same argv. Difference: under `legacy`, `envs` enter execd's outer shell before the wrapper
    clears them, so variables that change outer shell behavior (`LEGACY_UNSAFE_ENV`) are rejected as `invalid`.
    """
    check_env_keys(proc.env)
    if settings.protocol == "legacy" and (unsafe := sorted(LEGACY_UNSAFE_ENV & set(proc.env))):
        raise FlotillaError(
            f"under legacy execd these variables would alter execd's outer shell and cannot be passed: {unsafe}",
            stage="run",
            category=ErrorCategory.INVALID,
            retryable=False,
        )
    argv = wrap_argv(proc.argv, list(proc.env), wrapper=settings.wrapper)
    body: dict[str, Any] = {"cwd": proc.cwd, "background": background, "uid": proc.uid, "gid": proc.gid}
    if settings.protocol == "legacy":
        body["command"] = shlex.join(argv)
    else:
        body["argv"] = argv
    if proc.env:
        body["envs"] = dict(proc.env)
    if proc.timeout_s is not None:
        body["timeout"] = math.ceil(proc.timeout_s * 1000)
    return body
