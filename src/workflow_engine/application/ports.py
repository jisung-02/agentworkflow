from typing import Protocol

from workflow_engine.application.dto import TaskRequestDTO, TaskResultDTO


class Runner(Protocol):
    def run(self, request: TaskRequestDTO) -> TaskResultDTO: ...


class Clock(Protocol):
    def now_epoch(self) -> float: ...
