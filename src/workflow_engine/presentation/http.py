"""Signed Slack and Discord interaction endpoints."""

import json
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from workflow_engine.application.channel_commands import ChannelController
from workflow_engine.application.dto import ChannelCommandDTO
from workflow_engine.infrastructure.channel_auth import verify_discord, verify_slack
from workflow_engine.infrastructure.unsafe_boundary import load_json
from workflow_engine.infrastructure.yaml_definition import _mapping


def _discord_text(payload: dict[str, object]) -> str:
    data = _mapping(payload.get("data"), "discord data")
    options = data.get("options")
    if not isinstance(options, list):
        raise ValueError("discord command needs a text option")
    for raw in options:
        option = _mapping(raw, "discord option")
        if option.get("name") == "text" and isinstance(option.get("value"), str):
            return str(option["value"])
    raise ValueError("discord command needs a text option")


def _discord_actor(payload: dict[str, object]) -> str:
    member = payload.get("member")
    if isinstance(member, dict):
        user = _mapping(_mapping(member, "member").get("user"), "member.user")
    else:
        user = _mapping(payload.get("user"), "user")
    actor = user.get("id")
    if not isinstance(actor, str):
        raise ValueError("discord actor id is missing")
    return actor


def create_app(
    controller: ChannelController,
    slack_signing_secret: str | None,
    discord_public_key: str | None,
) -> FastAPI:
    app = FastAPI(title="Workflow Engine Channels")

    @app.post("/slack/command")
    async def slack_command(request: Request) -> JSONResponse:
        body = await request.body()
        if slack_signing_secret is None or not verify_slack(
            slack_signing_secret,
            request.headers.get("X-Slack-Request-Timestamp", ""),
            request.headers.get("X-Slack-Signature", ""),
            body,
        ):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        actor = form.get("user_id", [""])[0]
        message_id = form.get("trigger_id", [""])[0]
        if not actor or not message_id:
            return JSONResponse({"error": "missing command identity"}, status_code=400)
        command = ChannelCommandDTO(
            source="slack",
            actor_id=actor,
            message_id=message_id,
            text=form.get("text", [""])[0],
            channel_id=form.get("channel_id", [None])[0],
        )
        result = controller.handle(command)
        return JSONResponse({"response_type": "ephemeral", "text": result.text})

    @app.post("/discord/interactions")
    async def discord_interaction(request: Request) -> JSONResponse:
        body = await request.body()
        if discord_public_key is None or not verify_discord(
            discord_public_key,
            request.headers.get("X-Signature-Timestamp", ""),
            request.headers.get("X-Signature-Ed25519", ""),
            body,
        ):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            payload = _mapping(load_json(body.decode("utf-8")), "discord interaction")
            if payload.get("type") == 1:
                return JSONResponse({"type": 1})
            if payload.get("type") != 2:
                raise ValueError("unsupported interaction type")
            command = ChannelCommandDTO(
                source="discord",
                actor_id=_discord_actor(payload),
                message_id=str(payload.get("id", "")),
                text=_discord_text(payload),
                channel_id=str(payload["channel_id"])
                if isinstance(payload.get("channel_id"), str)
                else None,
            )
            result = controller.handle(command)
            return JSONResponse({"type": 4, "data": {"content": result.text, "flags": 64}})
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"type": 4, "data": {"content": str(exc), "flags": 64}})

    return app
