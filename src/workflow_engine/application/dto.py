from dataclasses import dataclass
from typing import Literal

from workflow_engine.domain.value_objects import RunId, TokenId

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
