"""Application service that advances durable workflow tokens."""

import time
from collections.abc import Mapping
from pathlib import Path

from workflow_engine.application.dto import RunStatusDTO, StartRunCommand, TaskRequestDTO
from workflow_engine.application.ports import Runner, WorkflowStore
from workflow_engine.domain.errors import VisitLimitError
from workflow_engine.domain.model import Definition
from workflow_engine.domain.value_objects import RunId, TokenId


class QuotaExceeded(RuntimeError):
    def __init__(self, wake_at: float) -> None:
        self.wake_at = wake_at
        super().__init__(f"quota available at {wake_at}")


class WorkflowEngine:
    def __init__(self, store: WorkflowStore, runners: Mapping[str, Runner], workdir: Path) -> None:
        self.store = store
        self.runners = runners
        self.workdir = workdir

    def start(self, definition: Definition, command: StartRunCommand) -> RunId:
        if command.definition_id != definition.id:
            raise ValueError("command definition does not match")
        if set(dict(command.inputs)) != set(definition.inputs):
            raise ValueError("inputs must match the definition")
        digest = self.store.save_definition(definition)
        return self.store.create_run(digest, command.inputs, command.idempotency_key)

    def status(self, run_id: RunId) -> RunStatusDTO:
        return self.store.status(run_id)

    def step(self, run_id: RunId) -> bool:
        token = self.store.next_ready(run_id)
        if token is None:
            return False
        definition = self.store.definition_for_run(run_id)
        state = definition.state(token.state_id)
        try:
            if state.kind == "end":
                if state.result is None:
                    raise ValueError("end state has no result")
                return self.store.end(token, state.result)
            if state.kind == "fork":
                if state.join is None:
                    raise ValueError("fork state has no join")
                limits = tuple(
                    (target, definition.state(target).max_visits) for _, target in state.branches
                )
                return self.store.fork(token, state.join, state.branches, limits)
            if state.kind == "join":
                success = state.destination("completed")
                failure = state.destination("failed")
                return self.store.arrive_join(
                    token,
                    success,
                    failure,
                    definition.state(success).max_visits,
                    definition.state(failure).max_visits,
                )
            if state.kind == "wait":
                return self.store.wait_for_input(token)
            if state.kind == "task":
                claimed = self.store.claim(token)
                if claimed is None:
                    return False
                if state.runner is None or state.instructions is None:
                    self.store.mark_attention(claimed)
                    return True
                runner = self.runners.get(state.runner)
                if runner is None:
                    self.store.mark_attention(claimed)
                    return True
                request = TaskRequestDTO(
                    run_id=run_id,
                    token_id=token.id,
                    state_id=state.id,
                    instructions=state.instructions,
                    inputs=self.store.inputs_for_run(run_id),
                    outputs=self.store.outputs_for_run(run_id),
                    workdir=str(self.workdir),
                    branch_id=token.branch_id,
                    outcomes=state.outcomes,
                )
                try:
                    result = runner.run(request)
                except QuotaExceeded as exc:
                    return self.store.quota_wait(claimed, exc.wake_at)
                except Exception:
                    self.store.mark_attention(claimed)
                    raise
                if result.outcome not in state.outcomes:
                    self.store.mark_attention(claimed)
                    raise ValueError(f"runner returned undeclared outcome: {result.outcome}")
                target = state.destination(result.outcome)
                return self.store.move(
                    claimed,
                    result.outcome,
                    target,
                    definition.state(target).max_visits,
                    state.output,
                    result.output,
                )
            raise ValueError(f"unknown state kind: {state.kind}")
        except VisitLimitError:
            self.store.mark_attention(self.store.token(token.id))
            return True

    def run_until_idle(self, run_id: RunId, max_steps: int = 1000) -> RunStatusDTO:
        self.store.wake_due(time.time())
        for _ in range(max_steps):
            if not self.step(run_id):
                return self.store.status(run_id)
        raise RuntimeError("step budget exceeded")

    def resume(self, run_id: RunId) -> RunStatusDTO:
        self.store.recover_inflight(run_id)
        return self.run_until_idle(run_id)

    def submit_input(self, token_id: TokenId, outcome: str) -> bool:
        token = self.store.token(token_id)
        if token.status != "waiting_input":
            return False
        definition = self.store.definition_for_run(token.run_id)
        state = definition.state(token.state_id)
        if state.kind != "wait" or outcome not in state.outcomes:
            raise ValueError("outcome is not valid for the waiting state")
        target = state.destination(outcome)
        try:
            return self.store.submit_input(
                token, outcome, target, definition.state(target).max_visits
            )
        except VisitLimitError:
            self.store.mark_attention(self.store.token(token.id))
            return True
