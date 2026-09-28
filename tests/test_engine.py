import sqlite3
from pathlib import Path

import pytest

from workflow_engine.application.dto import StartRunCommand, TaskRequestDTO, TaskResultDTO
from workflow_engine.application.engine import QuotaExceeded, WorkflowEngine
from workflow_engine.domain.value_objects import ArtifactRef
from workflow_engine.infrastructure.clock import SystemClock
from workflow_engine.infrastructure.file_artifacts import FileArtifactStore
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


class FailOnceRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("specific runner failure")
        return TaskResultDTO(outcome="completed", output="recovered")


class ReviewRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        self.calls += 1
        return TaskResultDTO(outcome="completed", output=f"review round {self.calls}")


class RecordingRunner:
    def __init__(self) -> None:
        self.requests: list[TaskRequestDTO] = []

    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        self.requests.append(request)
        return TaskResultDTO("completed", request.state_id)


class FailArtifactOnce:
    def __init__(self, root: Path) -> None:
        self.inner = FileArtifactStore(root)
        self.calls = 0

    def put_text(self, text: str) -> ArtifactRef:
        self.calls += 1
        if self.calls == 1:
            raise OSError("artifact disk full")
        return self.inner.put_text(text)


def _engine(
    tmp_path: Path, runner: EchoRunner | QuotaOnceRunner | FailOnceRunner | ReviewRunner
) -> WorkflowEngine:
    return WorkflowEngine(
        SQLiteStore(tmp_path / "run.db"),
        {"echo": runner},
        tmp_path,
        SystemClock(),
        FileArtifactStore(tmp_path / "artifacts"),
    )


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
    assert {name for name, _ in status.artifacts} == {
        "plan",
        "implementation",
        "documentation",
    }
    for name, artifact in status.artifacts:
        assert Path(artifact.path).read_text(encoding="utf-8") == dict(status.outputs)[name]
    assert restarted.resume(run_id).status == "success"


def test_parallel_branches_only_see_fork_snapshot_and_own_outputs(tmp_path: Path) -> None:
    (tmp_path / "task.md").write_text("Do work", encoding="utf-8")
    source = tmp_path / "branches.yaml"
    source.write_text(
        """version: 1
id: branches
entry: fork
states:
  fork:
    kind: fork
    branches: {left: left_first, right: right_task}
    join: gather
  left_first:
    kind: task
    runner: echo
    instructions: task.md
    output: left_output
    outcomes: [completed]
    on: {completed: left_second}
  left_second:
    kind: task
    runner: echo
    instructions: task.md
    outcomes: [completed]
    on: {completed: gather}
  right_task:
    kind: task
    runner: echo
    instructions: task.md
    output: right_output
    outcomes: [completed]
    on: {completed: gather}
  gather:
    kind: join
    policy: all_success
    on: {completed: done, failed: failed}
  done: {kind: end, result: success}
  failed: {kind: end, result: failure}
""",
        encoding="utf-8",
    )
    definition = parse_definition(source)
    runner = RecordingRunner()
    engine = WorkflowEngine(
        SQLiteStore(tmp_path / "run.db"),
        {"echo": runner},
        tmp_path,
        SystemClock(),
        FileArtifactStore(tmp_path / "artifacts"),
    )
    run_id = engine.start(definition, StartRunCommand(definition.id, ()))
    assert engine.run_until_idle(run_id).status == "success"
    requests = {request.state_id: request for request in runner.requests}
    assert dict(requests["left_first"].outputs) == {}
    assert dict(requests["left_second"].outputs) == {"left_output": "left_first"}
    assert dict(requests["right_task"].outputs) == {}
    assert dict(engine.status(run_id).outputs)["left_output"] == "left_first"
    assert dict(engine.status(run_id).outputs)["right_output"] == "right_task"


def test_artifact_failure_is_recorded_and_resume_does_not_rerun_task(tmp_path: Path) -> None:
    definition = parse_definition(EXAMPLE)
    runner = RecordingRunner()
    artifacts = FailArtifactOnce(tmp_path / "artifacts")
    engine = WorkflowEngine(
        SQLiteStore(tmp_path / "run.db"),
        {"echo": runner},
        tmp_path,
        SystemClock(),
        artifacts,
    )
    run_id = engine.start(definition, StartRunCommand(definition.id, (("request", "build"),)))
    with pytest.raises(OSError, match="artifact disk full"):
        engine.run_until_idle(run_id)
    status = engine.status(run_id)
    assert status.status == "needs_attention"
    assert "artifact disk full" in dict(status.attention)["plan"]
    restarted = WorkflowEngine(
        SQLiteStore(tmp_path / "run.db"),
        {"echo": runner},
        tmp_path,
        SystemClock(),
        artifacts,
    )
    assert restarted.resume(run_id).status == "success"
    assert [item.state_id for item in runner.requests].count("plan") == 1


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
    assert _engine(tmp_path, EchoRunner()).resume(run_id).status == "running"
    with sqlite3.connect(tmp_path / "run.db") as conn:
        conn.execute("UPDATE tokens SET lease_until=0 WHERE id=?", (token.id.value,))
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


def test_task_result_survives_visit_limit(tmp_path: Path) -> None:
    source = tmp_path / "task-loop.yaml"
    (tmp_path / "task.md").write_text("Do work", encoding="utf-8")
    source.write_text(
        """version: 1
id: task-loop
entry: task
inputs:
  request: {type: string, required: true}
states:
  task:
    kind: task
    runner: echo
    instructions: task.md
    output: review
    max_visits: 2
    outcomes: [completed]
    on: {completed: task}
""",
        encoding="utf-8",
    )
    definition = parse_definition(source)
    engine = _engine(tmp_path, ReviewRunner())
    run_id = engine.start(definition, StartRunCommand(definition.id, (("request", "build"),)))
    assert engine.run_until_idle(run_id).status == "needs_attention"
    status = engine.status(run_id)
    assert dict(status.outputs)["review"] == "review round 2"
    assert Path(dict(status.artifacts)["review"].path).read_text() == "review round 2"
    assert "max_visits=2" in dict(status.attention)["task"]
    assert engine.resume(run_id).status == "needs_attention"


def test_resume_retries_runner_error_and_keeps_diagnostic(tmp_path: Path) -> None:
    definition = parse_definition(EXAMPLE)
    runner = FailOnceRunner()
    engine = _engine(tmp_path, runner)
    run_id = engine.start(definition, StartRunCommand(definition.id, (("request", "build"),)))
    with pytest.raises(RuntimeError, match="specific runner failure"):
        engine.run_until_idle(run_id)
    status = engine.status(run_id)
    assert status.status == "needs_attention"
    assert "specific runner failure" in dict(status.attention)["plan"]
    assert engine.resume(run_id).status == "success"
    assert runner.calls == 4
