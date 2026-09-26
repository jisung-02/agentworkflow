import sqlite3
from pathlib import Path

from workflow_engine.application.dto import StartRunCommand, TaskRequestDTO, TaskResultDTO
from workflow_engine.application.engine import WorkflowEngine
from workflow_engine.application.notifications import NotificationService
from workflow_engine.application.timer import TimerService
from workflow_engine.infrastructure.clock import SystemClock
from workflow_engine.infrastructure.cron_schedule import CronNextFire
from workflow_engine.infrastructure.file_artifacts import FileArtifactStore
from workflow_engine.infrastructure.sqlite_store import SQLiteStore
from workflow_engine.infrastructure.yaml_definition import parse_definition
from workflow_engine.presentation.cli import _run_cycle

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "example-workflow.yaml"


class ConditionalRunner:
    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        if dict(request.inputs)["request"] == "bad":
            raise RuntimeError("runner failed")
        return TaskResultDTO("completed", request.state_id)


def _services(tmp_path: Path) -> tuple[WorkflowEngine, TimerService, NotificationService]:
    store = SQLiteStore(tmp_path / "state.db")
    clock = SystemClock()
    engine = WorkflowEngine(
        store,
        {"echo": ConditionalRunner()},
        tmp_path,
        clock,
        FileArtifactStore(tmp_path / "artifacts"),
    )
    return engine, TimerService(store, store, CronNextFire(), clock), NotificationService(store, {})


def test_worker_continues_after_one_runner_fails(tmp_path: Path) -> None:
    engine, timer, notifications = _services(tmp_path)
    definition = parse_definition(EXAMPLE)
    bad = engine.start(definition, StartRunCommand(definition.id, (("request", "bad"),)))
    good = engine.start(definition, StartRunCommand(definition.id, (("request", "good"),)))
    _run_cycle(engine, timer, notifications)
    assert engine.status(bad).status == "needs_attention"
    assert engine.status(good).status == "success"


def test_worker_recovers_expired_inflight_without_manual_resume(tmp_path: Path) -> None:
    engine, timer, notifications = _services(tmp_path)
    definition = parse_definition(EXAMPLE)
    run_id = engine.start(definition, StartRunCommand(definition.id, (("request", "x"),)))
    token = engine.store.next_ready(run_id)
    assert token is not None
    claimed = engine.store.claim(token)
    assert claimed is not None
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute("UPDATE tokens SET lease_until=0 WHERE id=?", (token.id.value,))
    _run_cycle(engine, timer, notifications)
    assert engine.status(run_id).status == "needs_attention"
