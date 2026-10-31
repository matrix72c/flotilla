"""OpenSandbox exec channel: exec / start_process / process_status / write_file / read_file.

Runs against `httpx.MockTransport`. Focus: NDJSON event parsing, line-wise output reconstruction, exit
code/timeout normalization, `env -i` restoring the environment by name only (values go via `envs`), no
command retries plus an overall deadline, fixed-length multipart upload (avoiding streamed chunking),
downloading bytes; `command` quoting and post-upload `chown` for the `legacy` protocol. See
docs/backends/opensandbox.md.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from flotilla.platform.base import ErrorCategory, ExecOutcomeUnknown, FlotillaError, ProcessSpec
from flotilla.platform.opensandbox.execd import (
    BUSYBOX,
    Execd,
    ExecdSettings,
    check_env_keys,
    command_body,
    fold_exec,
    identity_prefix,
    multipart_upload,
    parse_events,
    wrap_argv,
)
from flotilla.platform.opensandbox.http import Http, RetryPolicy
from flotilla.testing.platform import ManualClock

Handler = Callable[[httpx.Request], httpx.Response]


CURRENT = ExecdSettings()
LEGACY = ExecdSettings(protocol="legacy")


def _execd(handler: Handler, clock: ManualClock, settings: ExecdSettings = CURRENT) -> Execd:
    client = httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler))
    return Execd(Http(client, clock, RetryPolicy(attempts=3, initial_s=1.0, cap_s=4.0)), settings)


def _sse(*events: str) -> str:
    """Join JSON events into an execd NDJSON stream (bare JSON lines separated by blank lines)."""
    return "".join(f"{e}\n\n" for e in events)


def _proc(argv: tuple[str, ...], *, env: dict[str, str] | None = None, timeout_s: float | None = 30.0) -> ProcessSpec:
    return ProcessSpec(argv=argv, uid=1000, gid=1000, cwd="/work", env=env or {}, timeout_s=timeout_s)


# ───────────────────────────── Pure functions: event parsing and exit code normalization ─────────────────────────────


def test_parse_events_ndjson() -> None:
    text = _sse('{"type":"init","text":"cmd-1"}', '{"type":"stdout","text":"hello\\n"}')
    assert parse_events(text) == [{"type": "init", "text": "cmd-1"}, {"type": "stdout", "text": "hello\n"}]


def test_parse_events_tolerates_sse_prefix_and_comment() -> None:
    text = ': keep-alive\ndata: {"type":"ping"}\n\n{"type":"stdout","text":"x"}\n\n'
    assert parse_events(text) == [{"type": "ping"}, {"type": "stdout", "text": "x"}]


def test_parse_events_non_json_line_is_stdout() -> None:
    with pytest.raises(ExecOutcomeUnknown):
        parse_events("plain text line\n\n")


def test_fold_exec_accumulates_and_completes() -> None:
    events = parse_events(
        _sse(
            '{"type":"init","text":"c"}',
            '{"type":"stdout","text":"out1"}',
            '{"type":"stderr","text":"err"}',
            '{"type":"stdout","text":"out2"}',
            '{"type":"execution_complete","execution_time":5}',
        )
    )
    result = fold_exec(events)
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout == b"out1\nout2\n" and result.stderr == b"err\n"


def test_fold_exec_restores_lines() -> None:
    # NDJSON output shape: `printf 'a\n\nb\rc\nd'` yields events a, "\n", b, c, d (one per line with line
    # endings stripped; blank lines send "\n").
    texts = ["a", "\n", "b", "c", "d"]
    events = [{"type": "stdout", "text": t} for t in texts] + [{"type": "execution_complete"}]
    # Lossy reconstruction: \r becomes \n and the last line gets \n; splitting into lines matches the original.
    assert fold_exec(events).stdout == b"a\n\nb\nc\nd\n"
    assert fold_exec(events).stdout.decode().splitlines() == ["a", "", "b", "c", "d"]


def test_fold_exec_keeps_terminated_lines() -> None:
    # execd 1.1.1+ keeps each line's terminator; it must not be doubled.
    texts = ["a\n", "\n", "b\r", "c\r\n", "d"]
    events = [{"type": "stdout", "text": t} for t in texts] + [{"type": "stderr", "text": "e\n"}]
    result = fold_exec([*events, {"type": "execution_complete"}])
    assert result.stdout == b"a\n\nb\rc\r\nd\n"
    assert result.stdout.decode().splitlines() == ["a", "", "b", "c", "d"]
    assert result.stderr == b"e\n"


def test_fold_exec_error_exit_code() -> None:
    events = parse_events(_sse('{"type":"error","error":{"ename":"CommandExecError","evalue":"7"}}'))
    result = fold_exec(events)
    assert result.exit_code == 7 and not result.timed_out


def test_fold_exec_flat_error_value() -> None:
    # Legacy flat evalue is also accepted (execution.go backward compatibility).
    assert fold_exec(parse_events(_sse('{"type":"error","evalue":"3"}'))).exit_code == 3


def test_fold_exec_timeout_is_negative_exit() -> None:
    # execd kills the process group on timeout, ExitCode()=-1; normalized to timed_out.
    result = fold_exec(parse_events(_sse('{"type":"error","error":{"evalue":"-1"}}')))
    assert result.exit_code == -1 and not result.timed_out  # A signal alone does not establish its cause.


def test_fold_exec_truncated_stream_is_failure() -> None:
    # Neither error nor execution_complete: stream truncated, treated as failure exit code 1.
    with pytest.raises(ExecOutcomeUnknown) as error:
        fold_exec(parse_events(_sse('{"type":"stdout","text":"partial"}')))
    assert error.value.stdout == b"partial\n"
    assert not error.value.retryable


def test_fold_exec_noninteger_error_value() -> None:
    with pytest.raises(ExecOutcomeUnknown):
        fold_exec(parse_events(_sse('{"type":"error","error":{"evalue":"boom"}}')))


# ───────────────────────────── Pure functions: argv wrapping (variable names only) ─────────────────────────────


def test_wrap_argv_env_i_names_only() -> None:
    assert wrap_argv(("echo", "hi"), ["B", "A", "HOME"], wrapper=BUSYBOX) == [
        BUSYBOX,
        "sh",
        "-c",
        f'exec {BUSYBOX} env -i A="$A" B="$B" HOME="$HOME" "$@"',  # keys sorted; only names in the script
        "flotilla",
        "echo",
        "hi",
    ]


def test_wrap_argv_empty_env() -> None:
    assert "env -i" in wrap_argv(("true",), [], wrapper=BUSYBOX)[3]


def test_wrap_argv_uses_configured_wrapper() -> None:
    argv = wrap_argv(("ls",), [], wrapper="/opt/bb")
    assert argv[0] == "/opt/bb" and BUSYBOX not in argv[3]


@pytest.mark.parametrize(
    "bad",
    [
        {"protocol": "v2"},
        {"wrapper": "busybox"},  # relative path
        {"wrapper": "/opt/my bin/busybox"},  # spaces would be word-split by the shell in the wrapper script
        {"wrapper": "/opt/$X/busybox"},
        {"wrapper": "/opt/bb;id"},
        {"wrapper": "/opt/../bb"},  # not normalized
    ],
)
def test_execd_settings_rejects_bad_values(bad: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ExecdSettings(**bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["A=B", "", "A\0B", "-u", "FLOTILLA_ENV_0"])
def test_check_env_keys_rejects_names_docker_or_env_cannot_take(key: str) -> None:
    with pytest.raises(FlotillaError) as exc:
        check_env_keys({key: "v"})
    assert exc.value.category is ErrorCategory.INVALID


def test_check_env_keys_accepts_docker_style_names() -> None:
    check_env_keys({"PATH": "/bin", "_x1": "", "LC_ALL": "C", "discovery.type": "single-node", "APT::Get::X": "1"})
    check_env_keys({"1A": "", "A-B": "", "A B": "", "A$B": "", 'A"': ""})


def test_names_sh_cannot_reference_travel_under_aliases() -> None:
    body = command_body(
        _proc(("true",), env={"K": "1", "discovery.type": "single-node"}), background=False, settings=CURRENT
    )
    assert body["envs"] == {"K": "1", "FLOTILLA_ENV_0": "single-node"}
    assert "single-node" not in json.dumps(body["argv"])  # values only in envs


# ───────────────────────────── Pure functions: multipart encoding ─────────────────────────────


def test_multipart_upload_fixed_length_two_parts() -> None:
    content_type, body = multipart_upload("/etc/hosts", b"127.0.0.1 x\n", 0o644)
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="metadata"' in body and b"application/json" in body
    assert b'{"path": "/etc/hosts", "mode": 644}' in body  # mode is the octal digits read as decimal
    assert b'name="file"' in body and b"application/octet-stream" in body
    assert b"127.0.0.1 x\n" in body
    assert body.endswith(b"--\r\n")  # closing delimiter


# ───────────────────────────── exec (SSE stream + no retry) ─────────────────────────────


@pytest.mark.asyncio
async def test_exec_streams_and_folds(clock: ManualClock) -> None:
    posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse(
                '{"type":"init","text":"cmd-9"}',
                '{"type":"stdout","text":"ok"}',
                '{"type":"execution_complete"}',
            ),
        )

    secret = "s3cr'et value"
    result = await clock.run(_execd(handler, clock).exec(_proc(("echo", "ok"), env={"K": secret}, timeout_s=5.0)))
    assert result.exit_code == 0 and result.stdout == b"ok\n"
    # One request; values only in envs, only names in argv; timeout in milliseconds; carries uid/gid.
    assert len(posts) == 1
    assert posts[0]["envs"] == {"K": secret}
    assert posts[0]["argv"] == wrap_argv(("echo", "ok"), ["K"], wrapper=BUSYBOX)
    assert secret not in json.dumps(posts[0]["argv"]) and "command" not in posts[0]
    assert posts[0]["timeout"] == 35000 and posts[0]["uid"] == 1000 and posts[0]["background"] is False


@pytest.mark.asyncio
async def test_exec_without_env_omits_envs(clock: ManualClock) -> None:
    posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(200, content=_sse('{"type":"execution_complete"}'))

    await clock.run(_execd(handler, clock).exec(_proc(("true",))))
    assert "envs" not in posts[0]


@pytest.mark.asyncio
async def test_exec_bad_env_key_sends_nothing(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_sse('{"type":"execution_complete"}'))

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock).exec(_proc(("true",), env={"BAD=KEY": "v"})))
    assert calls == 0 and exc.value.category is ErrorCategory.INVALID


@pytest.mark.asyncio
async def test_exec_stream_bounded_by_deadline(clock: ManualClock) -> None:
    # execd keeps sending pings and never finishes: the read timeout never fires, so the overall deadline
    # timeout_s + HTTP_SLACK_S ends it (via the injected clock).
    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"type":"ping"}\n\n'
            await clock.sleep(10_000)
            yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stalled())

    started = clock.monotonic()
    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock).exec(_proc(("sleep", "999"), timeout_s=5.0)))
    assert exc.value.category is ErrorCategory.TRANSIENT and exc.value.stage == "run"
    assert clock.monotonic() - started == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_exec_requires_timeout(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse('{"type":"execution_complete"}'))

    with pytest.raises(ValueError, match="timeout"):
        await clock.run(_execd(handler, clock).exec(_proc(("x",), timeout_s=None)))


@pytest.mark.asyncio
async def test_exec_command_not_retried(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock).exec(_proc(("x",))))
    assert calls == 1 and exc.value.category is ErrorCategory.TRANSIENT  # command execution is not idempotent


# ───────────────────────────── start_process / process_status ─────────────────────────────


@pytest.mark.asyncio
async def test_start_process_returns_init_id(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["background"] is True
        return httpx.Response(200, content=_sse('{"type":"init","text":"bg-42"}', '{"type":"execution_complete"}'))

    pid = await clock.run(_execd(handler, clock).start_process(_proc(("server",), timeout_s=None)))
    assert pid == "bg-42"


@pytest.mark.asyncio
async def test_process_status_running(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/command/status/bg-42"
        return httpx.Response(200, json={"id": "bg-42", "running": True, "exit_code": None})

    status = await clock.run(_execd(handler, clock).process_status("bg-42"))
    assert status.found and status.running and status.exit_code is None


@pytest.mark.asyncio
async def test_process_status_finished(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "bg-42", "running": False, "exit_code": 0})

    status = await clock.run(_execd(handler, clock).process_status("bg-42"))
    assert status.found and not status.running and status.exit_code == 0


@pytest.mark.asyncio
async def test_process_status_404_is_not_found(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "gone"})

    status = await clock.run(_execd(handler, clock).process_status("bg-x"))
    assert status == type(status)(found=False, running=False, exit_code=None)


# ───────────────────────────── write_file / read_file ─────────────────────────────


@pytest.mark.asyncio
async def test_write_file_fixed_length_multipart_to_identity_route(clock: ManualClock) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["ctype"] = request.headers["Content-Type"]
        captured["clen"] = request.headers.get("Content-Length")
        captured["body"] = bytes(request.content)
        return httpx.Response(200)

    data = b"hello world"
    await clock.run(_execd(handler, clock).write_file("/srv/f.txt", data, mode=0o644, uid=1000, gid=1000))
    assert captured["path"] == f"{identity_prefix(1000, 1000)}/files/upload"
    assert str(captured["ctype"]).startswith("multipart/form-data; boundary=")
    # Fixed length: explicit Content-Length equal to the in-memory body (not streamed/chunked).
    assert captured["clen"] == str(len(captured["body"]))  # type: ignore[arg-type]
    assert data in captured["body"]  # type: ignore[operator]
    assert b'{"path": "/srv/f.txt", "mode": 644}' in captured["body"]  # type: ignore[operator]


@pytest.mark.asyncio
async def test_write_file_not_retried(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(FlotillaError):
        await clock.run(_execd(handler, clock).write_file("/f", b"x", mode=0o644, uid=0, gid=0))
    assert calls == 1  # overwrite is not idempotent


@pytest.mark.asyncio
async def test_read_file_returns_bytes(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/files/download"
        assert request.url.params["path"] == "/etc/hosts"
        return httpx.Response(200, content=b"127.0.0.1 localhost\n")

    data = await clock.run(_execd(handler, clock).read_file("/etc/hosts"))
    assert data == b"127.0.0.1 localhost\n"


@pytest.mark.asyncio
async def test_read_file_404_is_not_found(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "no file"})

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock).read_file("/missing"))
    assert exc.value.category is ErrorCategory.NOT_FOUND


# ───────────────────────────── legacy protocol ─────────────────────────────


HOSTILE_ARGS = (
    "",  # empty argument
    "a b",
    "tab\there",
    "nl\nx",
    "crlf\r\n",
    "back\\\nslash",
    "-n",
    "--",
    "$HOME",
    "${HOME}",
    "$(id)",
    "`id`",
    "!!",
    "~",
    "a=~",
    "*",
    "{a,b}",
    "#c",
    "'\"",
    "non-ASCII café-ünïcode\u2028😀",
)


@pytest.mark.parametrize("shell", ["/bin/sh", "/bin/bash"])
def test_legacy_command_reproduces_argv_and_env_in_a_real_shell(shell: str, tmp_path: Path) -> None:
    """execd's legacy mode runs `<shell> -c <command>` with the request envs layered over execd's environment:
    verify against a real shell that one round of word splitting yields the original argv, and that the
    process environment is exactly `proc.env`.

    The wrapper is a script forwarding sh / env / awk / id to the system tools instead of busybox.
    """
    if not Path(shell).exists():
        pytest.skip(f"no {shell}")
    bb = tmp_path / "bb"
    bb.write_text(
        '#!/bin/sh\napp=$1; shift\ncase $app in sh) exec /bin/sh "$@";; env) exec /usr/bin/env "$@";;\n'
        '  awk) exec /usr/bin/awk "$@";; id) exec /usr/bin/id "$@";; esac\nexit 99\n'
    )
    bb.chmod(0o755)
    settings = ExecdSettings(protocol="legacy", wrapper=str(bb))
    for env in (
        {"K": "s3cr'et $(id) `id` value", "PATH": "/usr/bin:/bin", "HOME": "/explicit"},
        {"K": "s3cr'et", "discovery.type": "single-node $(id)", "APT::Get::X": "true", "a b'c": "v", "PATH": "/bin"},
    ):

        def run(argv: tuple[str, ...], env: dict[str, str] = env) -> list[str]:
            body = command_body(_proc(argv, env=env), background=False, settings=settings)
            assert "canary" not in body["command"] and "s3cr" not in body["command"]  # values only in envs
            noisy = {"PATH": "/usr/bin:/bin", "IFS": "x", "NOISE": "1", **body["envs"]}
            out = subprocess.run([shell, "-c", body["command"]], env=noisy, capture_output=True, check=True).stdout
            return out.decode().split("\0")[:-1]

        assert run(("/usr/bin/printf", "%s\\0", *HOSTILE_ARGS)) == list(HOSTILE_ARGS)
        expected = [f"{k}={v}" for k, v in env.items()]
        if "HOME" not in env:
            expected.append(f"HOME={_passwd_home()}")
        assert sorted(run(("/usr/bin/env", "-0"))) == sorted(expected)


def _passwd_home() -> str:
    """HOME the wrapper resolves for the current uid when the request sets none."""
    import pwd

    try:
        return pwd.getpwuid(os.getuid()).pw_dir or "/"
    except KeyError:
        return "/"


def test_legacy_and_current_bodies_differ_only_in_command_field() -> None:
    proc = _proc(("true",), env={"A": "1"}, timeout_s=2.0)
    current = command_body(proc, background=True, settings=CURRENT)
    legacy = command_body(proc, background=True, settings=LEGACY)
    assert current.pop("argv") == shlex.split(legacy.pop("command"))
    assert (
        current
        == legacy
        == {"cwd": "/work", "background": True, "uid": 1000, "gid": 1000, "envs": {"A": "1"}, "timeout": 2000}
    )


@pytest.mark.asyncio
async def test_legacy_write_file_as_root_uses_plain_route_without_chown(clock: ManualClock) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200)

    await clock.run(_execd(handler, clock, LEGACY).write_file("/etc/hosts", b"x", mode=0o644, uid=0, gid=0))
    assert paths == ["/files/upload"]


@pytest.mark.asyncio
async def test_legacy_write_file_non_root_chowns_then_restores_mode(clock: ManualClock) -> None:
    calls: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files/upload":
            calls.append(("upload", bytes(request.content)))
            return httpx.Response(200)
        calls.append(("command", json.loads(request.content)))
        return httpx.Response(200, content=_sse('{"type":"execution_complete"}'))

    await clock.run(_execd(handler, clock, LEGACY).write_file("/srv/a b.txt", b"data", mode=0o4750, uid=1000, gid=1001))
    assert [kind for kind, _ in calls] == ["upload", "command"]
    assert b'{"path": "/srv/a b.txt", "mode": 4750}' in calls[0][1]  # type: ignore[operator]
    cmd = calls[1][1]
    assert isinstance(cmd, dict) and cmd["uid"] == 0 and cmd["gid"] == 0 and "envs" not in cmd
    # Original argv after wrapping: chown and chmod via busybox (root changing the owner clears the setuid
    # bit, so mode must be reset); paths with spaces are safe too.
    inner = shlex.split(cmd["command"])
    inner = inner[inner.index("flotilla") + 1 :]
    assert inner == [
        BUSYBOX,
        "sh",
        "-c",
        '"$0" chown "$1" "$3" && "$0" chmod "$2" "$3"',
        BUSYBOX,
        "1000:1001",
        "4750",
        "/srv/a b.txt",
    ]


@pytest.mark.asyncio
async def test_legacy_write_file_chown_failure_is_invalid(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files/upload":
            return httpx.Response(200)
        return httpx.Response(
            200,
            content=_sse(
                '{"type":"stderr","text":"chown: Operation not permitted"}', '{"type":"error","error":{"evalue":"1"}}'
            ),
        )

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock, LEGACY).write_file("/f", b"x", mode=0o600, uid=1000, gid=1000))
    assert exc.value.category is ErrorCategory.INVALID and not exc.value.retryable
    assert "Operation not permitted" in str(exc.value)


@pytest.mark.asyncio
async def test_legacy_write_file_upload_failure_skips_chown(clock: ManualClock) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(500)

    with pytest.raises(FlotillaError):
        await clock.run(_execd(handler, clock, LEGACY).write_file("/f", b"x", mode=0o600, uid=1000, gid=1000))
    assert paths == ["/files/upload"]  # no retry, and no chown after the failure


@pytest.mark.parametrize("path", ["rel", "/srv/$HOME/f", "/srv/${X}"])
@pytest.mark.asyncio
async def test_legacy_write_file_rejects_relative_or_expandable_path(clock: ManualClock, path: str) -> None:
    # Legacy upload expands `$VAR` using execd's environment while the following chown takes the literal
    # path: the two steps would hit different files.
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    with pytest.raises(ValueError):
        await clock.run(_execd(handler, clock, LEGACY).write_file(path, b"", mode=0o600, uid=1000, gid=1000))
    assert calls == 0


@pytest.mark.parametrize("key", ["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"])
def test_legacy_rejects_env_that_alters_outer_shell(key: str) -> None:
    # E.g. SHELLOPTS=noexec makes the outer bash silently return 0 and a health check falsely pass; the
    # current protocol has no outer shell and is unaffected.
    with pytest.raises(FlotillaError) as exc:
        command_body(_proc(("true",), env={key: "x"}), background=False, settings=LEGACY)
    assert exc.value.category is ErrorCategory.INVALID
    command_body(_proc(("true",), env={key: "x"}), background=False, settings=CURRENT)


@pytest.mark.asyncio
async def test_legacy_write_file_chown_timeout_is_transient(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files/upload":
            return httpx.Response(200)
        return httpx.Response(200, content=_sse('{"type":"error","error":{"evalue":"-1"}}'))

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_execd(handler, clock, LEGACY).write_file("/f", b"x", mode=0o600, uid=1000, gid=1000))
    assert exc.value.category is ErrorCategory.TRANSIENT and exc.value.retryable


@pytest.mark.asyncio
async def test_download_stops_oversized_stream_without_retry(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"too large")

    async with httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler)) as client:
        http = Http(client, clock, RetryPolicy(attempts=3))
        with pytest.raises(FlotillaError, match="transfer limit") as caught:
            await http.download("/files/download", stage="run", max_bytes=4)
        assert caught.value.category is ErrorCategory.INVALID
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["current", "legacy"])
async def test_command_id_is_observed_before_stream_finishes(protocol: str) -> None:
    import asyncio
    from typing import Literal, cast

    from flotilla.platform.base import ExecAttempt
    from flotilla.platform.clock import SystemClock

    tail = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"type":"init","text":"specific-id"}\n\n'
            await tail.wait()
            yield b'{"type":"execution_complete"}\n\n'

    async with httpx.AsyncClient(
        base_url="http://execd.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())),
    ) as client:
        execd = Execd(Http(client, SystemClock()), ExecdSettings(protocol=cast(Literal["current", "legacy"], protocol)))
        attempt = ExecAttempt()
        task = asyncio.create_task(execd.exec(_proc(("true",)), attempt=attempt))
        async with asyncio.timeout(1):
            while attempt.command_id is None:
                await asyncio.sleep(0.001)
        assert attempt.sent and attempt.command_id == "specific-id" and not task.done()
        tail.set()
        assert (await task).exit_code == 0


@pytest.mark.asyncio
async def test_execution_output_is_bounded_without_fabricating_exit(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1024)

    with pytest.raises(ExecOutcomeUnknown, match="limit"):
        await clock.run(_execd(handler, clock, ExecdSettings(output_limit_bytes=128)).exec(_proc(("noisy",))))


@pytest.mark.parametrize("protocol", ["current", "legacy"])
@pytest.mark.parametrize("background", [False, True])
def test_home_defaults_follow_execution_user_and_preserve_overrides(
    protocol: str, background: bool, tmp_path: Path
) -> None:
    """Execute the wire command, with passwd/id fixtures standing in for the sandbox."""
    uid_file = tmp_path / "uid"
    passwd = tmp_path / "passwd"
    passwd.write_text("root:x:0:0::/root:/bin/sh\nagent:x:1000:1000::/home/agent:/bin/sh\n")
    bb = tmp_path / "bb"
    bb.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\nfrom pathlib import Path\n"
        "applet, *args = sys.argv[1:]\n"
        f"if applet == 'id': print(Path({str(uid_file)!r}).read_text().strip()); sys.exit(0)\n"
        f"if applet == 'awk': args[-1] = {str(passwd)!r}\n"
        "exe = {'sh': '/bin/sh', 'env': '/usr/bin/env', 'awk': '/usr/bin/awk'}[applet]\n"
        "os.execv(exe, [exe, *args])\n"
    )
    bb.chmod(0o755)
    settings = ExecdSettings(protocol=protocol, wrapper=str(bb))  # type: ignore[arg-type]

    def execute(uid: int, explicit: dict[str, str] | None = None) -> dict[str, str]:
        uid_file.write_text(str(uid))
        proc = ProcessSpec(("/usr/bin/env", "-0"), uid, uid, "/", explicit or {}, 10)
        body = command_body(proc, background=background, settings=settings)
        argv = body.get("argv") or ["/bin/bash", "-c", body["command"]]
        result = subprocess.run(
            argv,
            env={"HOME": "/daemon-home", "EXECD_ACCESS_TOKEN": "must-not-leak", **body.get("envs", {})},
            capture_output=True,
            check=True,
        )
        return dict(entry.split("=", 1) for entry in result.stdout.decode().split("\0") if entry)

    assert execute(0) == {"HOME": "/root"}
    assert execute(1000) == {"HOME": "/home/agent"}
    assert execute(98765) == {"HOME": "/"}
    assert execute(1000, {"HOME": ""}) == {"HOME": ""}
    explicit = "/custom home;$(do-not-execute)"
    assert execute(0, {"HOME": explicit}) == {"HOME": explicit}
    passwd.write_text("agent:x:1000:1000::/changed home;$(literal):/bin/sh\n")
    assert execute(1000) == {"HOME": "/changed home;$(literal)"}
    passwd.unlink()
    assert execute(1000) == {"HOME": "/"}


@pytest.mark.asyncio
async def test_command_waiting_for_a_connection_is_not_sent_or_timed() -> None:
    # A command queued behind a full connection pool must not be counted as sent, and its timeout must not run
    # while it waits: otherwise it expires as "outcome unknown" without ever reaching the exec daemon.
    import asyncio

    from flotilla.platform.base import ExecAttempt
    from flotilla.platform.clock import SystemClock

    release = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        def __init__(self, hold: bool) -> None:
            self._hold = hold

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"type":"init","text":"cmd"}\n\n'
            if self._hold:
                await release.wait()
            yield b'{"type":"execution_complete"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stream(hold=b"first" in request.content))

    async with httpx.AsyncClient(base_url="http://execd.test", transport=httpx.MockTransport(handler)) as client:
        execd = Execd(Http(client, SystemClock(), slots=asyncio.Semaphore(1)), CURRENT)
        first = asyncio.create_task(execd.exec(_proc(("echo", "first"))))
        await asyncio.sleep(0.01)
        attempt = ExecAttempt()
        queued = asyncio.create_task(execd.exec(_proc(("echo", "second"), timeout_s=0.05), attempt=attempt))
        await asyncio.sleep(0.3)  # well past the queued command's own timeout
        assert not queued.done() and not attempt.sent
        release.set()
        result = await asyncio.wait_for(queued, 1)
        assert result.exit_code == 0 and not result.timed_out
        assert (await first).exit_code == 0
