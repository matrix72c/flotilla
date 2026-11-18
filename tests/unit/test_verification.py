"""What a task's verification runs, computed from `task.toml` instead of read from a release."""

from __future__ import annotations

from types import SimpleNamespace

from flotilla.compose.verification import artifacts_of, verification_of

CONFIG = SimpleNamespace(verifier=SimpleNamespace(env={"MODE": "grade"}, timeout_sec=90.0))


def test_a_task_without_tests_has_no_verification_and_no_artifacts() -> None:
    """Nothing grades the attempt, so there is also nothing for a grader to read."""
    assert verification_of(CONFIG, roles={"main": object()}, has_tests=False) is None
    assert artifacts_of(CONFIG, has_tests=False) == ()


def test_verification_runs_in_the_verifier_role_when_the_task_declares_one() -> None:
    result = verification_of(CONFIG, roles={"main": object(), "verifier": object()}, has_tests=True)
    assert result is not None
    assert result.role == "verifier"


def test_verification_shares_the_agents_role_without_a_separate_environment() -> None:
    """A task with one environment is asking for its grader to run where the agent worked."""
    result = verification_of(CONFIG, roles={"main": object()}, has_tests=True)
    assert result is not None
    assert result.role == "main"


def test_verification_carries_the_tasks_own_command_env_and_timeout() -> None:
    result = verification_of(CONFIG, roles={"main": object()}, has_tests=True)
    assert result is not None
    assert result.command == ("bash", "/tests/test.sh")
    assert (result.tests, result.tests_target) == ("tests", "/tests")
    assert result.env == {"MODE": "grade"}
    assert result.timeout_s == 90.0


def test_a_reward_may_be_written_as_text_or_json_and_neither_is_required_on_disk() -> None:
    """Output is required, but a verifier may express the outcome through its exit status alone."""
    result = verification_of(CONFIG, roles={"main": object()}, has_tests=True)
    assert result is not None
    assert result.require_output is True
    assert result.output_paths == {}
    assert set(result.optional_output_paths) == {"reward.txt", "reward.json"}


def test_the_agents_log_directory_is_the_one_artifact_handed_to_the_verifier() -> None:
    artifacts = artifacts_of(CONFIG, has_tests=True)
    assert [(a.name, a.role, a.target) for a in artifacts] == [("agent", "main", "/logs/agent")]
