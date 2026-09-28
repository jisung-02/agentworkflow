from dataclasses import dataclass
from typing import Literal

from workflow_engine.domain.value_objects import ArtifactRef, RunId, TokenId

RunStatus = Literal[
    "running",
    "waiting_input",
    "waiting_quota",
    "waiting_host",
    "needs_attention",
    "success",
    "failure",
]


@dataclass(frozen=True, slots=True)
class StartRunCommand:
    definition_id: str
    inputs: tuple[tuple[str, str], ...]
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRequestDTO:
    run_id: RunId
    token_id: TokenId
    state_id: str
    instructions: str
    inputs: tuple[tuple[str, str], ...]
    outputs: tuple[tuple[str, str], ...]
    workdir: str
    branch_id: str | None = None
    outcomes: tuple[str, ...] = ()
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskResultDTO:
    outcome: str
    output: str = ""
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class RunStatusDTO:
    run_id: RunId
    definition_id: str
    status: RunStatus
    active_states: tuple[str, ...]
    outputs: tuple[tuple[str, str], ...]
    artifacts: tuple[tuple[str, ArtifactRef], ...] = ()
    attention: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class TokenDTO:
    id: TokenId
    run_id: RunId
    state_id: str
    status: str
    fork_id: str | None
    branch_id: str | None
    last_outcome: str | None
    version: int
    external_id: str | None


@dataclass(frozen=True, slots=True)
class ScheduleDTO:
    id: str
    definition_hash: str
    cron: str
    timezone: str
    inputs: tuple[tuple[str, str], ...]
    due_at: float


@dataclass(frozen=True, slots=True)
class ChannelCommandDTO:
    source: str
    actor_id: str
    message_id: str
    text: str
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChannelResponseDTO:
    text: str


@dataclass(frozen=True, slots=True)
class NotificationDTO:
    id: str
    source: str
    channel_id: str
    text: str
