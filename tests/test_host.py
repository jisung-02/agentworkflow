import json
import sqlite3
from pathlib import Path

import pytest

from workflow_engine.application.dto import StartRunCommand
from workflow_engine.application.engine import WorkflowEngine
from workflow_engine.infrastructure.clock import SystemClock
from workflow_engine.infrastructure.file_artifacts import FileArtifactStore
from workflow_engine.infrastructure.sqlite_store import SQLiteStore
from workflow_engine.infrastructure.yaml_definition import parse_definition
from workflow_engine.presentation.cli import main


def test_host_handoff_survives_restart(tmp_path: Path) -> None:
    (tmp_path / "instructions.md").write_text("Review the result", encoding="utf-8")
    path = tmp_path / "host.yaml"
    path.write_text(
        """version: 1
id: host
entry: review
states:
  review:
    kind: task
    runner: claude-host
    instructions: instructions.md
    output: result
    outcomes: [approved, rejected]
    on: {approved: done, rejected: failed}
  done: {kind: end, result: success}
  failed: {kind: end, result: failure}
""",
        encoding="utf-8",
    )
    definition = parse_definition(path)
    store = SQLiteStore(tmp_path / "state.db")
    engine = WorkflowEngine(
        store,
        {},
        tmp_path,
        SystemClock(),
        FileArtifactStore(tmp_path / "artifacts"),
    )
    run_id = engine.start(definition, StartRunCommand(definition.id, ()))
    store.bind_channel(run_id, "slack", "U1", "C1")
    assert engine.run_until_idle(run_id).status == "waiting_host"
    notices = store.pending_notifications()
    assert len(notices) == 1
    assert "호스트 작업 대기" in notices[0].text
    restarted = WorkflowEngine(
        SQLiteStore(tmp_path / "state.db"),
        {},
        tmp_path,
        SystemClock(),
        FileArtifactStore(tmp_path / "artifacts"),
    )
    task = restarted.host_next("claude-host")
    assert task is not None
    assert task.instructions == "Review the result"
    assert restarted.host_next("claude-host") is None
    assert restarted.host_complete(task.token_id, "approved", "looks good")
    assert not restarted.host_complete(task.token_id, "approved", "duplicate")
    assert restarted.run_until_idle(run_id).status == "success"
    assert dict(restarted.status(run_id).outputs)["result"] == "looks good"


def test_host_cli_claims_and_completes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "instructions.md").write_text("Check it", encoding="utf-8")
    definition = tmp_path / "cli.yaml"
    definition.write_text(
        """version: 1
id: cli-host
entry: task
states:
  task:
    kind: task
    runner: claude-host
    instructions: instructions.md
    output: review
    outcomes: [approved]
    on: {approved: done}
  done: {kind: end, result: success}
""",
        encoding="utf-8",
    )
    db = tmp_path / "state.db"
    assert main(["--db", str(db), "run", str(definition)]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "host-next", "--runner", "claude-host"]) == 0
    task = json.loads(capsys.readouterr().out)
    assert task["instructions"] == "Check it"
    output = tmp_path / "output.txt"
    output.write_text("approved", encoding="utf-8")
    assert (
        main(
            [
                "--db",
                str(db),
                "host-complete",
                task["token_id"],
                "approved",
                "--output-file",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "success"


def test_resume_requeues_expired_host_claim(tmp_path: Path) -> None:
    instruction = tmp_path / "instructions.md"
    instruction.write_text("Check it", encoding="utf-8")
    definition_path = tmp_path / "host.yaml"
    definition_path.write_text(
        """version: 1
id: host-expiry
entry: task
states:
  task:
    kind: task
    runner: claude-host
    instructions: instructions.md
    output: review
    outcomes: [approved]
    on: {approved: done}
  done: {kind: end, result: success}
""",
        encoding="utf-8",
    )
    db = tmp_path / "state.db"
    engine = WorkflowEngine(
        SQLiteStore(db), {}, tmp_path, SystemClock(), FileArtifactStore(tmp_path / "artifacts")
    )
    definition = parse_definition(definition_path)
    run_id = engine.start(definition, StartRunCommand(definition.id, ()))
    assert engine.run_until_idle(run_id).status == "waiting_host"
    task = engine.host_next("claude-host")
    assert task is not None
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE tokens SET lease_until=0 WHERE id=?", (task.token_id.value,))
    assert engine.store.recover_expired_inflight(engine.clock.now_epoch()) == 1
    assert engine.resume(run_id).status == "waiting_host"
    assert engine.host_next("claude-host") is not None
