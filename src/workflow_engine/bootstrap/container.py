from pathlib import Path

from workflow_engine.application.engine import WorkflowEngine
from workflow_engine.application.ports import Runner
from workflow_engine.application.timer import TimerService
from workflow_engine.infrastructure.clock import SystemClock
from workflow_engine.infrastructure.cron_schedule import CronNextFire
from workflow_engine.infrastructure.runners.codex_cli import CodexCliRunner
from workflow_engine.infrastructure.runners.echo import EchoRunner
from workflow_engine.infrastructure.sqlite_store import SQLiteStore


def build_engine(database: Path, workdir: Path) -> WorkflowEngine:
    runners: dict[str, Runner] = {"echo": EchoRunner(), "codex": CodexCliRunner()}
    return WorkflowEngine(SQLiteStore(database), runners, workdir)


def build_timer_service(database: Path) -> TimerService:
    store = SQLiteStore(database)
    return TimerService(store, store, CronNextFire(), SystemClock())
