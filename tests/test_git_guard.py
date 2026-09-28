import subprocess
from pathlib import Path

from workflow_engine.application.dto import TaskRequestDTO, TaskResultDTO
from workflow_engine.domain.value_objects import RunId, TokenId
from workflow_engine.infrastructure.runners.git_guard import GitGuardRunner


class QaRunner:
    def __init__(self, path: Path, change: bool) -> None:
        self.path = path
        self.change = change

    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        if self.change:
            self.path.write_text("QA changed source", encoding="utf-8")
        return TaskResultDTO("approved", "tests passed")


def test_qa_guard_blocks_source_changes_after_existing_implementation(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    source = tmp_path / "main.go"
    source.write_text("package main", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "main.go"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    source.write_text("implementation", encoding="utf-8")
    request = TaskRequestDTO(
        RunId("run"),
        TokenId("token"),
        "qa",
        "Run tests",
        (),
        (),
        str(tmp_path),
        outcomes=("approved", "blocked"),
    )
    assert GitGuardRunner(QaRunner(source, change=False)).run(request).outcome == "approved"
    result = GitGuardRunner(QaRunner(source, change=True)).run(request)
    assert result.outcome == "blocked"
    assert "변경" in result.output
