"""Command line interface for local workflows."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from workflow_engine.application.dto import StartRunCommand
from workflow_engine.application.engine import WorkflowEngine
from workflow_engine.application.notifications import NotificationService
from workflow_engine.application.timer import TimerService
from workflow_engine.bootstrap.container import (
    build_channel_access_store,
    build_engine,
    build_notification_service,
    build_timer_service,
)
from workflow_engine.domain.model import Definition
from workflow_engine.domain.value_objects import RunId, TokenId
from workflow_engine.infrastructure.yaml_definition import DefinitionError, parse_definition


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workflow")
    parser.add_argument("--db", type=Path, default=Path(".workflow/state.db"))
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "graph", "run", "register"):
        command = commands.add_parser(name)
        command.add_argument("definition", type=Path)
        if name == "run":
            command.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
            command.add_argument("--key", help="idempotency key")
    for name in ("status", "resume"):
        commands.add_parser(name).add_argument("run_id")
    response = commands.add_parser("respond")
    response.add_argument("token_id")
    response.add_argument("outcome")
    host_next = commands.add_parser("host-next")
    host_next.add_argument("--runner", choices=("claude-host", "codex-host"), required=True)
    host_complete = commands.add_parser("host-complete")
    host_complete.add_argument("token_id")
    host_complete.add_argument("outcome")
    host_complete.add_argument("--output-file", type=Path, required=True)
    commands.add_parser("host-heartbeat").add_argument("token_id")
    commands.add_parser("tick")
    serve = commands.add_parser("serve")
    serve.add_argument("--interval", type=float, default=30.0)
    http = commands.add_parser("serve-http")
    http.add_argument("--definitions", type=Path, required=True)
    http.add_argument("--host", default="127.0.0.1")
    http.add_argument("--port", type=int, default=8080)
    return parser


def _inputs(items: list[str]) -> tuple[tuple[str, str], ...]:
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"input must be KEY=VALUE: {item}")
        key, value = item.split("=", 1)
        if not key or key in result:
            raise ValueError(f"input key is empty or duplicated: {key}")
        result[key] = value
    return tuple(result.items())


def _graph(definition: Definition) -> str:
    lines = ["stateDiagram-v2", f"    [*] --> {definition.entry}"]
    for state in definition.states:
        if state.kind == "end":
            lines.append(f"    {state.id} --> [*]")
        for outcome, target in state.on:
            lines.append(f"    {state.id} --> {target}: {outcome}")
        for branch, target in state.branches:
            lines.append(f"    {state.id} --> {target}: {branch}")
    return "\n".join(lines)


def _print_status(engine: WorkflowEngine, run_id: RunId) -> None:
    status = engine.status(run_id)
    waiting = engine.store.waiting_tokens(run_id)
    definition = engine.store.definition_for_run(run_id)
    print(
        json.dumps(
            {
                "run_id": run_id.value,
                "definition_id": status.definition_id,
                "status": status.status,
                "active_states": status.active_states,
                "outputs": dict(status.outputs),
                "attention": dict(status.attention),
                "artifacts": {
                    name: {"digest": artifact.digest, "path": artifact.path}
                    for name, artifact in status.artifacts
                },
                "waiting_tokens": [
                    {
                        "id": item.id.value,
                        "state": item.state_id,
                        "prompt": definition.state(item.state_id).prompt,
                    }
                    for item in waiting
                ],
            },
            ensure_ascii=False,
        )
    )


def _run_cycle(
    engine: WorkflowEngine, timer: TimerService, notifications: NotificationService
) -> None:
    timer.fire_due()
    now = engine.clock.now_epoch()
    engine.store.wake_due(now)
    engine.store.recover_expired_inflight(now)
    for run_id in engine.store.ready_run_ids():
        try:
            engine.run_until_idle(run_id)
            _print_status(engine, run_id)
        except Exception as exc:
            print(f"workflow: run {run_id.value} failed: {exc}", file=sys.stderr)
    try:
        notifications.deliver_pending()
    except Exception as exc:
        print(f"workflow: notification delivery failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            definition = parse_definition(args.definition)
            print(f"valid: {definition.id} ({len(definition.states)} states)")
            return 0
        if args.command == "graph":
            print(_graph(parse_definition(args.definition)))
            return 0
        if args.command == "register":
            count = build_timer_service(args.db).register(parse_definition(args.definition))
            print(f"registered {count} timer(s)")
            return 0
        if args.command == "serve-http":
            import uvicorn

            from workflow_engine.application.channel_commands import ChannelController
            from workflow_engine.infrastructure.actor_policy import AllowListPolicy
            from workflow_engine.infrastructure.definition_catalog import (
                FilesystemDefinitionCatalog,
            )
            from workflow_engine.presentation.http import create_app

            slack_secret = os.getenv("WORKFLOW_SLACK_SIGNING_SECRET")
            discord_key = os.getenv("WORKFLOW_DISCORD_PUBLIC_KEY")
            actors = frozenset(
                item.strip()
                for item in os.getenv("WORKFLOW_ALLOWED_ACTORS", "").split(",")
                if item.strip()
            )
            if not (slack_secret or discord_key) or not actors:
                raise ValueError("channel secret and WORKFLOW_ALLOWED_ACTORS are required")
            engine = build_engine(args.db, args.workdir)
            controller = ChannelController(
                engine,
                FilesystemDefinitionCatalog(args.definitions),
                AllowListPolicy(actors),
                build_channel_access_store(args.db),
            )
            app = create_app(controller, slack_secret, discord_key)
            uvicorn.run(app, host=args.host, port=args.port)
            return 0
        engine = build_engine(args.db, args.workdir)
        if args.command == "run":
            definition = parse_definition(args.definition)
            run_id = engine.start(
                definition,
                StartRunCommand(definition.id, _inputs(args.input), args.key),
            )
            engine.run_until_idle(run_id)
            _print_status(engine, run_id)
            return 0
        if args.command == "status":
            _print_status(engine, RunId(args.run_id))
            return 0
        if args.command == "resume":
            run_id = RunId(args.run_id)
            engine.resume(run_id)
            _print_status(engine, run_id)
            return 0
        if args.command == "respond":
            token_id = TokenId(args.token_id)
            token = engine.store.token(token_id)
            if not engine.submit_input(token_id, args.outcome):
                raise ValueError("response was already applied or token is not waiting")
            engine.run_until_idle(token.run_id)
            _print_status(engine, token.run_id)
            return 0
        if args.command == "host-next":
            request = engine.host_next(args.runner)
            if request is None:
                print("null")
            else:
                print(
                    json.dumps(
                        {
                            "run_id": request.run_id.value,
                            "token_id": request.token_id.value,
                            "state_id": request.state_id,
                            "instructions": request.instructions,
                            "inputs": dict(request.inputs),
                            "outputs": dict(request.outputs),
                            "workdir": request.workdir,
                            "outcomes": request.outcomes,
                        },
                        ensure_ascii=False,
                    )
                )
            return 0
        if args.command == "host-complete":
            token_id = TokenId(args.token_id)
            token = engine.store.token(token_id)
            output = args.output_file.read_text(encoding="utf-8")
            if not engine.host_complete(token_id, args.outcome, output):
                raise ValueError("host task was already completed")
            engine.run_until_idle(token.run_id)
            _print_status(engine, token.run_id)
            return 0
        if args.command == "host-heartbeat":
            if not engine.store.renew_lease(TokenId(args.token_id)):
                raise ValueError("token is not executing")
            print("lease renewed")
            return 0
        if args.command in ("tick", "serve"):
            if args.command == "serve" and args.interval <= 0:
                raise ValueError("interval must be positive")
            timer = build_timer_service(args.db)
            notifications = build_notification_service(
                args.db,
                os.getenv("WORKFLOW_SLACK_BOT_TOKEN"),
                os.getenv("WORKFLOW_DISCORD_BOT_TOKEN"),
            )
            while True:
                _run_cycle(engine, timer, notifications)
                if args.command == "tick":
                    return 0
                time.sleep(args.interval)
    except (DefinitionError, ValueError, OSError) as exc:
        print(f"workflow: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
