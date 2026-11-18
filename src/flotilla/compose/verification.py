"""What a task's verification runs, and which outputs the agent hands it.

This is the part of a task that is not a Compose project: which role grades the attempt, what it
executes, where it writes a reward, and which agent directory it may read. Preparation used to
compute these three values and write them into a release; they are a pure function of the task's
own `task.toml`, so they are computed where they are used instead.

`Verification` and `Artifact` keep their shapes — the runtime consumes them unchanged — but they
are values in memory now, not a persisted schema with a version to migrate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flotilla.native.models import Artifact, Verification

#: Where the verification script lives inside the task directory, and where it is mounted.
TESTS_DIR = "tests"
TESTS_TARGET = "/tests"
#: The agent's own output directory, handed to the verifier read-only.
AGENT_ARTIFACT = "agent"
AGENT_TARGET = "/logs/agent"
#: Where the verifier sees it. A separate path, because the verifier writes its own `/logs/...`
#: and must not have the agent's directory mounted over part of it.
AGENT_HANDOFF_TARGET = "/handoff/agent"

#: A reward the verifier may write, either as a bare number or as JSON. Optional, because a
#: verifier is also allowed to express the outcome through its exit status alone.
REWARD_OUTPUTS = {
    "reward.txt": "/logs/verifier/reward.txt",
    "reward.json": "/logs/verifier/reward.json",
}


def verification_of(config: Any, *, roles: Mapping[str, Any], has_tests: bool) -> Verification | None:
    """How this task is graded, or None when it ships no verification script.

    The verifier runs in its own role when the task declares a separate verifier environment, and
    in `main` otherwise — in which case it shares the sandbox the agent just worked in, which is
    what a task without a separate environment is asking for.
    """
    if not has_tests:
        return None
    return Verification(
        role="verifier" if "verifier" in roles else "main",
        command=("bash", f"{TESTS_TARGET}/test.sh"),
        tests=TESTS_DIR,
        tests_target=TESTS_TARGET,
        optional_output_paths=dict(REWARD_OUTPUTS),
        require_output=True,
        env=dict(config.verifier.env),
        timeout_s=config.verifier.timeout_sec,
    )


def artifacts_of(config: Any, *, has_tests: bool) -> tuple[Artifact, ...]:
    """The directories the agent writes that outlive its sandbox.

    One, always the same: the agent's log directory, which the verifier reads to grade the run.
    A task that declares its own `artifacts` is refused during import rather than represented
    here, so there is nothing else to collect.
    """
    del config  # reserved: a task-declared artifact list is refused at import, not encoded here
    if not has_tests:
        return ()
    return (Artifact(name=AGENT_ARTIFACT, role="main", target=AGENT_TARGET, verifier_target=AGENT_HANDOFF_TARGET),)
