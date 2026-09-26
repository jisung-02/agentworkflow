"""Adapter for an already authenticated local Codex CLI installation."""

import json
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from workflow_engine.application.dto import TaskRequestDTO, TaskResultDTO
from workflow_engine.application.engine import QuotaExceeded
from workflow_engine.infrastructure.unsafe_boundary import load_json
from workflow_engine.infrastructure.yaml_definition import _mapping, _string

ProcessCall = Callable[..., subprocess.CompletedProcess[str]]


def _quota_reset(text: str) -> float | None:
    lowered = text.lower()
    if not any(word in lowered for word in ("usage limit", "rate limit", "quota")):
        return None
    match = re.search(r"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", text)
    if match is not None:
        try:
            return datetime.fromisoformat(match.group().replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + 15 * 60


def _thread_id(events: str) -> str | None:
    for line in events.splitlines():
        try:
            event = _mapping(load_json(line), "codex event")
        except (ValueError, json.JSONDecodeError):
            continue
        thread_id = event.get("thread_id")
        if event.get("type") == "thread.started" and isinstance(thread_id, str):
            return thread_id
    return None


class CodexCliRunner:
    def __init__(
        self,
        executable: str = "codex",
        process: ProcessCall = subprocess.run,
        timeout_seconds: int = 3600,
    ) -> None:
        self.executable = executable
        self.process = process
        self.timeout_seconds = timeout_seconds

    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        if request.branch_id is not None:
            raise RuntimeError("Codex code tasks in parallel branches require isolated worktrees")
        workdir = Path(request.workdir).resolve()
        if not workdir.is_dir():
            raise ValueError(f"workdir does not exist: {workdir}")
        schema = {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": list(request.outcomes)},
                "output": {"type": "string"},
            },
            "required": ["outcome", "output"],
            "additionalProperties": False,
        }
        prompt = (
            f"State: {request.state_id}\nInstructions:\n{request.instructions}\n\n"
            f"Inputs (JSON): {json.dumps(dict(request.inputs), ensure_ascii=False)}\n"
            f"Previous outputs (JSON): {json.dumps(dict(request.outputs), ensure_ascii=False)}\n\n"
            "Finish with a JSON object matching the supplied output schema. "
            "Use an allowed outcome and summarize the work in output."
        )
        with tempfile.TemporaryDirectory(prefix="workflow-codex-") as directory:
            schema_path = Path(directory) / "schema.json"
            output_path = Path(directory) / "last.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                self.executable,
                "exec",
                "--json",
                "--sandbox",
                "workspace-write",
                "--cd",
                str(workdir),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            completed = self.process(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                reset = _quota_reset(completed.stderr + "\n" + completed.stdout)
                if reset is not None:
                    raise QuotaExceeded(reset)
                raise RuntimeError(
                    f"codex exec exited {completed.returncode}: {completed.stderr[-500:]}"
                )
            if not output_path.is_file():
                raise RuntimeError("codex exec produced no final message")
            data = _mapping(load_json(output_path.read_text(encoding="utf-8")), "codex result")
            outcome = _string(data.get("outcome"), "codex result.outcome")
            if outcome not in request.outcomes:
                raise ValueError(f"codex returned undeclared outcome: {outcome}")
            output = data.get("output")
            if not isinstance(output, str):
                raise ValueError("codex result.output must be a string")
            return TaskResultDTO(
                outcome=outcome, output=output, external_id=_thread_id(completed.stdout)
            )
