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
    ) -> subprocess.CompletedProcess[str]:
        self.command = command
        assert "Inputs (JSON)" in input
        assert text and capture_output and not check and timeout > 0
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


def test_codex_cli_rejects_unisolated_parallel_branch(tmp_path: Path) -> None:
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
    with pytest.raises(RuntimeError, match="worktrees"):
        CodexCliRunner(process=FakeProcess()).run(parallel)
