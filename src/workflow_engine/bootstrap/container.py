from pathlib import Path

from workflow_engine.application.engine import WorkflowEngine
from workflow_engine.application.notifications import NotificationService
from workflow_engine.application.ports import NotificationSender, NotificationStore, Runner
from workflow_engine.application.timer import TimerService
from workflow_engine.infrastructure.channel_sender import DiscordBotSender, SlackBotSender
from workflow_engine.infrastructure.clock import SystemClock
from workflow_engine.infrastructure.cron_schedule import CronNextFire
from workflow_engine.infrastructure.file_artifacts import FileArtifactStore
from workflow_engine.infrastructure.runners.codex_cli import CodexCliRunner
from workflow_engine.infrastructure.runners.echo import EchoRunner
from workflow_engine.infrastructure.sqlite_store import SQLiteStore
from workflow_engine.infrastructure.unsafe_boundary import load_runner_plugins


def build_engine(database: Path, workdir: Path) -> WorkflowEngine:
    runners: dict[str, Runner] = {
        "echo": EchoRunner(),
        "codex": CodexCliRunner(),
        "codex-review": CodexCliRunner(sandbox_mode="read-only"),
    }
    for name, runner in load_runner_plugins().items():
        if name in runners or name in ("claude-host", "codex-host"):
            raise ValueError(f"runner name is reserved: {name}")
        runners[name] = runner
    return WorkflowEngine(
        SQLiteStore(database),
        runners,
        workdir,
        SystemClock(),
        FileArtifactStore(database.parent / "artifacts"),
    )


def build_timer_service(database: Path) -> TimerService:
    store = SQLiteStore(database)
    return TimerService(store, store, CronNextFire(), SystemClock())


def build_notification_service(
    database: Path, slack_token: str | None, discord_token: str | None
) -> NotificationService:
    senders: dict[str, NotificationSender] = {}
    if slack_token:
        senders["slack"] = SlackBotSender(slack_token)
    if discord_token:
        senders["discord"] = DiscordBotSender(discord_token)
    return NotificationService(SQLiteStore(database), senders)


def build_notification_store(database: Path) -> NotificationStore:
    return SQLiteStore(database)
