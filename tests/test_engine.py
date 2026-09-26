from pathlib import Path

from workflow_engine.application.dto import StartRunCommand, TaskRequestDTO, TaskResultDTO
from workflow_engine.application.engine import QuotaExceeded, WorkflowEngine
from workflow_engine.infrastructure.sqlite_store import SQLiteStore
from workflow_engine.infrastructure.yaml_definition import parse_definition

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "example-workflow.yaml"


class EchoRunner:
    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        return TaskResultDTO(
            outcome="completed", output=f"{request.state_id}:{dict(request.inputs)['request']}"
        )


class QuotaOnceRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        self.calls += 1
        if self.calls == 1:
            raise QuotaExceeded(0)
        return TaskResultDTO(outcome="completed", output=request.state_id)


def _engine(tmp_path: Path, runner: EchoRunner | QuotaOnceRunner) -> WorkflowEngine:
    return WorkflowEngine(SQLiteStore(tmp_path / "run.db"), {"echo": runner}, tmp_path)


def test_parallel_run_survives_restart(tmp_path: Path) -> None:
    definition = parse_definition(EXAMPLE)
    engine = _engine(tmp_path, EchoRunner())
    run_id = engine.start(definition, StartRunCommand(definition.id, (("request", "build"),)))
    assert engine.step(run_id)
    assert engine.step(run_id)

    restarted = _engine(tmp_path, EchoRunner())
    status = restarted.resume(run_id)
    assert status.status == "success"
    assert dict(status.outputs) == {
        "plan": "plan:build",
        "implementation": "implement:build",
        "documentation": "document:build",
    }
    assert restarted.resume(run_id).status == "success"


def test_claim_is_versioned_and_duplicate_is_ignored(tmp_path: Path) -> None:
    definition = parse_definition(EXAMPLE)
    engine = _engine(tmp_path, EchoRunner())
    run_id = engine.start(definition, StartRunCommand(definition.id, (("request", "build"),)))
    token = engine.store.next_ready(run_id)
    assert token is not None
    claimed = engine.store.claim(token)
    assert claimed is not None
    assert engine.store.claim(token) is None
    assert engine.store.move(claimed, "completed", "parallel", None, "plan", "done")
    assert not engine.store.move(claimed, "completed", "parallel", None, "plan", "duplicate")
    assert dict(engine.store.outputs_for_run(run_id))["plan"] == "done"


def test_quota_wait_wakes_and_resumes(tmp_path: Path) -> None:
    definition = parse_definition(EXAMPLE)
    runner = QuotaOnceRunner()
    engine = _engine(tmp_path, runner)
    run_id = engine.start(definition, StartRunCommand(definition.id, (("request", "build"),)))
    assert engine.run_until_idle(run_id).status == "waiting_quota"
    assert _engine(tmp_path, runner).run_until_idle(run_id).status == "success"
    assert runner.calls == 4


def test_interrupted_external_call_needs_attention(tmp_path: Path) -> None:
    definition = parse_definition(EXAMPLE)
    engine = _engine(tmp_path, EchoRunner())
    run_id = engine.start(definition, StartRunCommand(definition.id, (("request", "build"),)))
    token = engine.store.next_ready(run_id)
    assert token is not None
    assert engine.store.claim(token) is not None
    assert _engine(tmp_path, EchoRunner()).resume(run_id).status == "needs_attention"


def test_wait_state_accepts_one_response(tmp_path: Path) -> None:
    source = tmp_path / "wait.yaml"
    source.write_text(
        """version: 1
id: approval
entry: ask
states:
  ask:
    kind: wait
    prompt: Approve?
    outcomes: [yes, no]
    on: {yes: accepted, no: rejected}
  accepted: {kind: end, result: success}
  rejected: {kind: end, result: failure}
""",
        encoding="utf-8",
    )
    definition = parse_definition(source)
    engine = _engine(tmp_path, EchoRunner())
    run_id = engine.start(definition, StartRunCommand(definition.id, ()))
    assert engine.run_until_idle(run_id).status == "waiting_input"
    waiting = engine.store.waiting_tokens(run_id)[0].id
    assert engine.submit_input(waiting, "yes")
    assert not engine.submit_input(waiting, "yes")
    assert engine.run_until_idle(run_id).status == "success"


def test_visit_limit_stops_loop(tmp_path: Path) -> None:
    source = tmp_path / "loop.yaml"
    source.write_text(
        """version: 1
id: loop
entry: ask
states:
  ask:
    kind: wait
    prompt: Again?
    max_visits: 1
    outcomes: [again]
    on: {again: ask}
""",
        encoding="utf-8",
    )
    definition = parse_definition(source)
    engine = _engine(tmp_path, EchoRunner())
    run_id = engine.start(definition, StartRunCommand(definition.id, ()))
    assert engine.run_until_idle(run_id).status == "waiting_input"
    token_id = engine.store.waiting_tokens(run_id)[0].id
    assert engine.submit_input(token_id, "again")
    assert engine.status(run_id).status == "needs_attention"
