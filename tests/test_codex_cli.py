import json
import subprocess
from pathlib import Path

import pytest

from workflow_engine.application.dto import TaskRequestDTO
from workflow_engine.application.engine import QuotaExceeded
from workflow_engine.domain.value_objects import RunId, TokenId
from workflow_engine.infrastructure.runners.codex_cli import CodexCliRunner


class FakeProcess:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.command: list[str] = []

    def __call__(
        self,
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
        cwd: str,
    ) -> subprocess.CompletedProcess[str]:
        self.command = command
        assert "Inputs (JSON)" in input
        assert text and capture_output and not check and timeout > 0
        assert Path(cwd).is_dir()
        if self.returncode == 0:
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(json.dumps({"outcome": "completed", "output": "done"}))
        return subprocess.CompletedProcess(
            command,
            self.returncode,
            '{"type":"thread.started","thread_id":"session-1"}\n',
            self.stderr,
        )


def _request(tmp_path: Path) -> TaskRequestDTO:
    return TaskRequestDTO(
        RunId("run"),
        TokenId("token"),
        "plan",
        "Make a plan",
        (("request", "demo"),),
        (),
        str(tmp_path),
        outcomes=("completed",),
    )


def test_codex_cli_parses_structured_result(tmp_path: Path) -> None:
    fake = FakeProcess()
    result = CodexCliRunner(process=fake).run(_request(tmp_path))
    assert result.outcome == "completed"
    assert result.output == "done"
    assert result.external_id == "session-1"
    assert "--output-schema" in fake.command


def test_codex_cli_converts_usage_limit_to_wait(tmp_path: Path) -> None:
    fake = FakeProcess(returncode=1, stderr="Usage limit reached; resets at 2026-10-01T09:00:00Z")
    with pytest.raises(QuotaExceeded) as error:
        CodexCliRunner(process=fake).run(_request(tmp_path))
    assert error.value.wake_at > 0
    assert error.value.external_id == "session-1"


def test_codex_cli_resumes_saved_session(tmp_path: Path) -> None:
    fake = FakeProcess()
    request = _request(tmp_path)
    resumed = TaskRequestDTO(
        request.run_id,
        request.token_id,
        request.state_id,
        request.instructions,
        request.inputs,
        request.outputs,
        request.workdir,
        outcomes=request.outcomes,
        external_id="session-1",
    )
    CodexCliRunner(process=fake).run(resumed)
    assert fake.command[:3] == ["codex", "exec", "resume"]
    assert "session-1" in fake.command


def test_codex_cli_uses_isolated_parallel_worktree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    (tmp_path / "README.md").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    request = _request(tmp_path)
    parallel = TaskRequestDTO(
        request.run_id,
        request.token_id,
        request.state_id,
        request.instructions,
        request.inputs,
        request.outputs,
        request.workdir,
        branch_id="implementation",
        outcomes=request.outcomes,
    )
    result = CodexCliRunner(process=FakeProcess()).run(parallel)
    assert "Workflow worktree:" in result.output
    worktree = tmp_path.parent / f".{tmp_path.name}-workflow-worktrees" / "run" / "implementation"
    assert worktree.is_dir()
    assert (worktree / "README.md").read_text(encoding="utf-8") == "initial"
