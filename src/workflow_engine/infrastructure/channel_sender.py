"""Outgoing Slack and Discord bot messages for durable notifications."""

import json
from urllib.request import Request, urlopen

from workflow_engine.application.dto import NotificationDTO
from workflow_engine.infrastructure.unsafe_boundary import load_json
from workflow_engine.infrastructure.yaml_definition import _mapping


class SlackBotSender:
    def __init__(self, token: str) -> None:
        self.token = token

    def send(self, notification: NotificationDTO) -> None:
        body = json.dumps(
            {
                "channel": notification.channel_id,
                "text": notification.text,
                "client_msg_id": notification.id,
            }
        ).encode("utf-8")
        request = Request(
            "https://slack.com/api/chat.postMessage",
            data=body,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            result = _mapping(load_json(response.read().decode("utf-8")), "slack response")
        if result.get("ok") is not True:
            raise RuntimeError(f"Slack rejected notification: {result.get('error')}")


class DiscordBotSender:
    def __init__(self, token: str) -> None:
        self.token = token

    def send(self, notification: NotificationDTO) -> None:
        body = json.dumps({"content": notification.text}).encode("utf-8")
        request = Request(
            f"https://discord.com/api/v10/channels/{notification.channel_id}/messages",
            data=body,
            headers={"Authorization": f"Bot {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"Discord rejected notification: HTTP {response.status}")
