from pathlib import Path

from workflow_engine.application.engine import WorkflowEngine
from workflow_engine.application.timer import TimerService
from workflow_engine.infrastructure.cron_schedule import CronNextFire
from workflow_engine.infrastructure.file_artifacts import FileArtifactStore
from workflow_engine.infrastructure.runners.echo import EchoRunner
from workflow_engine.infrastructure.sqlite_store import SQLiteStore
from workflow_engine.infrastructure.yaml_definition import parse_definition


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def now_epoch(self) -> float:
        return self.now


def test_timer_fires_once_and_is_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "timer.yaml"
    instructions = tmp_path / "task.md"
    instructions.write_text("Do the task", encoding="utf-8")
    path.write_text(
        """version: 1
id: scheduled
entry: task
inputs:
  request: {type: string, required: true}
triggers:
  - type: timer
    cron: "* * * * *"
    timezone: UTC
    input: {request: daily}
states:
  task:
    kind: task
    runner: echo
    instructions: task.md
    output: result
    outcomes: [completed]
    on: {completed: done}
  done: {kind: end, result: success}
""",
        encoding="utf-8",
    )
    definition = parse_definition(path)
    store = SQLiteStore(tmp_path / "state.db")
    clock = FakeClock(1_700_000_000)
    timer = TimerService(store, store, CronNextFire(), clock)
    assert timer.register(definition) == 1
    schedule = store.due_schedules(1_800_000_000)[0]
    clock.now = schedule.due_at + 1
    run_ids = timer.fire_due()
    assert len(run_ids) == 1
    assert timer.fire_due() == ()
    engine = WorkflowEngine(
        store,
        {"echo": EchoRunner()},
        tmp_path,
        clock,
        FileArtifactStore(tmp_path / "artifacts"),
    )
    assert engine.run_until_idle(run_ids[0]).status == "success"
    assert dict(engine.status(run_ids[0]).outputs)["result"].endswith("request=daily")
