"""Application service that advances durable workflow tokens."""

from collections.abc import Mapping
from pathlib import Path
from threading import Event, Thread

from workflow_engine.application.dto import (
    RunStatusDTO,
    StartRunCommand,
    TaskRequestDTO,
    TaskResultDTO,
    TokenDTO,
)
from workflow_engine.application.ports import ArtifactStore, Clock, Runner, WorkflowStore
from workflow_engine.domain.errors import VisitLimitError
from workflow_engine.domain.model import Definition
from workflow_engine.domain.value_objects import RunId, TokenId


class QuotaExceeded(RuntimeError):
    def __init__(self, wake_at: float, external_id: str | None = None) -> None:
        self.wake_at = wake_at
        self.external_id = external_id
        super().__init__(f"quota available at {wake_at}")


class WorkflowEngine:
    def __init__(
        self,
        store: WorkflowStore,
        runners: Mapping[str, Runner],
        workdir: Path,
        clock: Clock,
        artifacts: ArtifactStore,
    ) -> None:
        self.store = store
        self.runners = runners
        self.workdir = workdir
        self.clock = clock
        self.artifacts = artifacts

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
                if state.prompt is None:
                    raise ValueError("wait state has no prompt")
                return self.store.wait_for_input(token, state.prompt)
            if state.kind == "task":
                if state.runner in ("claude-host", "codex-host"):
                    return self.store.wait_for_host(token)
                claimed = self.store.claim(token)
                if claimed is None:
                    return False
                if state.runner is None or state.instructions is None:
                    self.store.mark_attention(
                        claimed, "configuration", "runner or instructions missing"
                    )
                    return True
                runner = self.runners.get(state.runner)
                if runner is None:
                    self.store.mark_attention(
                        claimed, "configuration", f"unknown runner: {state.runner}"
                    )
                    return True
                request = self._task_request(claimed, state.instructions, state.outcomes)
                try:
                    result = self._run_with_heartbeat(runner, request)
                except QuotaExceeded as exc:
                    return self.store.quota_wait(claimed, exc.wake_at, exc.external_id)
                except Exception as exc:
                    self.store.mark_attention(claimed, "runner_error", str(exc))
                    raise
                if result.outcome not in state.outcomes:
                    self.store.mark_attention(
                        claimed, "runner_error", f"undeclared outcome: {result.outcome}"
                    )
                    raise ValueError(f"runner returned undeclared outcome: {result.outcome}")
                target = state.destination(result.outcome)
                artifact = (
                    self.artifacts.put_text(result.output) if state.output is not None else None
                )
                return self.store.move(
                    claimed,
                    result.outcome,
                    target,
                    definition.state(target).max_visits,
                    state.output,
                    result.output,
                    artifact,
                )
            raise ValueError(f"unknown state kind: {state.kind}")
        except VisitLimitError as exc:
            self.store.mark_attention(self.store.token(token.id), "visit_limit", str(exc))
            return True

    def run_until_idle(self, run_id: RunId, max_steps: int = 1000) -> RunStatusDTO:
        self.store.wake_due(self.clock.now_epoch())
        for _ in range(max_steps):
            if not self.step(run_id):
                return self.store.status(run_id)
        raise RuntimeError("step budget exceeded")

    def resume(self, run_id: RunId) -> RunStatusDTO:
        self.store.retry_attention(run_id)
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
        except VisitLimitError as exc:
            self.store.mark_attention(self.store.token(token.id), "visit_limit", str(exc))
            return True

    def host_next(self, runner_id: str) -> TaskRequestDTO | None:
        if runner_id not in ("claude-host", "codex-host"):
            raise ValueError("unknown host runner")
        for token in self.store.host_waiting():
            state = self.store.definition_for_run(token.run_id).state(token.state_id)
            if state.runner != runner_id or state.instructions is None:
                continue
            claimed = self.store.claim_host(token)
            if claimed is not None:
                return self._task_request(claimed, state.instructions, state.outcomes)
        return None

    def host_complete(self, token_id: TokenId, outcome: str, output: str) -> bool:
        token = self.store.token(token_id)
        if token.status != "executing":
            return False
        definition = self.store.definition_for_run(token.run_id)
        state = definition.state(token.state_id)
        if state.runner not in ("claude-host", "codex-host") or outcome not in state.outcomes:
            raise ValueError("host result does not match the waiting task")
        target = state.destination(outcome)
        try:
            return self.store.move(
                token,
                outcome,
                target,
                definition.state(target).max_visits,
                state.output,
                output,
                self.artifacts.put_text(output) if state.output is not None else None,
            )
        except VisitLimitError as exc:
            self.store.mark_attention(self.store.token(token.id), "visit_limit", str(exc))
            return True

    def _task_request(
        self, token: TokenDTO, instructions: str, outcomes: tuple[str, ...]
    ) -> TaskRequestDTO:
        return TaskRequestDTO(
            run_id=token.run_id,
            token_id=token.id,
            state_id=token.state_id,
            instructions=instructions,
            inputs=self.store.inputs_for_run(token.run_id),
            outputs=self.store.outputs_for_run(token.run_id),
            workdir=str(self.workdir),
            branch_id=token.branch_id,
            outcomes=outcomes,
            external_id=token.external_id,
        )

    def _run_with_heartbeat(self, runner: Runner, request: TaskRequestDTO) -> TaskResultDTO:
        stop = Event()

        def renew() -> None:
            while not stop.wait(60):
                if not self.store.renew_lease(request.token_id):
                    return

        heartbeat = Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            return runner.run(request)
        finally:
            stop.set()
            heartbeat.join(timeout=1)
