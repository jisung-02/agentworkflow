from dataclasses import dataclass
from typing import Literal

StateKind = Literal["task", "fork", "join", "wait", "end"]


@dataclass(frozen=True, slots=True)
class TimerTrigger:
    cron: str
    timezone: str
    inputs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class State:
    id: str
    kind: StateKind
    on: tuple[tuple[str, str], ...] = ()
    outcomes: tuple[str, ...] = ()
    runner: str | None = None
    instructions: str | None = None
    output: str | None = None
    branches: tuple[tuple[str, str], ...] = ()
    join: str | None = None
    policy: str | None = None
    result: str | None = None
    prompt: str | None = None
    max_visits: int | None = None

    def destination(self, outcome: str) -> str:
        for name, target in self.on:
            if name == outcome:
                return target
        raise ValueError(f"{self.id}: outcome {outcome!r} has no transition")


@dataclass(frozen=True, slots=True)
class Definition:
    id: str
    name: str
    entry: str
    inputs: tuple[str, ...]
    states: tuple[State, ...]
    timers: tuple[TimerTrigger, ...] = ()

    def state(self, state_id: str) -> State:
        for state in self.states:
            if state.id == state_id:
                return state
        raise ValueError(f"unknown state: {state_id}")
