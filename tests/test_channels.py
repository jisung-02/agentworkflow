import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from workflow_engine.application.channel_commands import ChannelController
from workflow_engine.application.dto import ChannelCommandDTO, NotificationDTO, StartRunCommand
from workflow_engine.application.notifications import NotificationService
from workflow_engine.bootstrap.container import build_engine
from workflow_engine.domain.value_objects import RunId
from workflow_engine.infrastructure.actor_policy import AllowListPolicy
from workflow_engine.infrastructure.definition_catalog import FilesystemDefinitionCatalog
from workflow_engine.infrastructure.sqlite_store import SQLiteStore
from workflow_engine.infrastructure.yaml_definition import parse_definition
from workflow_engine.presentation.http import create_app

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "example-workflow.yaml"


def _controller(tmp_path: Path) -> ChannelController:
    folder = tmp_path / "definitions"
    folder.mkdir()
    (folder / "example-workflow.yaml").write_text(EXAMPLE.read_text(encoding="utf-8"))
    (folder / "instructions").mkdir()
    for source in (EXAMPLE.parent / "instructions").glob("*.md"):
        (folder / "instructions" / source.name).write_text(source.read_text(encoding="utf-8"))
    engine = build_engine(tmp_path / "state.db", tmp_path)
    notifications = SQLiteStore(tmp_path / "state.db")
    return ChannelController(
        engine,
        FilesystemDefinitionCatalog(folder),
        AllowListPolicy(frozenset({"slack:U1", "slack:U2", "discord:D1"})),
        notifications,
    )


def test_channel_command_is_authorized_and_idempotent(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    denied = controller.handle(
        ChannelCommandDTO("slack", "U3", "one", "run example-workflow request=x")
    )
    assert "권한" in denied.text
    command = ChannelCommandDTO("slack", "U1", "one", "run example-workflow request=x")
    first = controller.handle(command)
    second = controller.handle(command)
    assert first == second
    assert "실행 등록" in first.text


def test_slack_endpoint_verifies_signature(tmp_path: Path) -> None:
    client = TestClient(create_app(_controller(tmp_path), "secret", None))
    body = urlencode(
        {"user_id": "U1", "trigger_id": "t-1", "text": "run example-workflow request=x"}
    )
    timestamp = str(int(time.time()))
    base = f"v0:{timestamp}:{body}".encode()
    signature = "v0=" + hmac.new(b"secret", base, hashlib.sha256).hexdigest()
    headers = {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature}
    response = client.post("/slack/command", content=body, headers=headers)
    assert response.status_code == 200
    assert "실행 등록" in response.json()["text"]
    assert client.post("/slack/command", content=body).status_code == 401


def test_discord_endpoint_verifies_signature_and_ping(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    public = (
        key.public_key()
        .public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
        .hex()
    )
    client = TestClient(create_app(_controller(tmp_path), None, public))
    body = b'{"type":1}'
    timestamp = str(int(time.time()))
    headers = {
        "X-Signature-Timestamp": timestamp,
        "X-Signature-Ed25519": key.sign(timestamp.encode() + body).hex(),
    }
    response = client.post("/discord/interactions", content=body, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"type": 1}
    assert client.post("/discord/interactions", content=body).status_code == 401


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[NotificationDTO] = []

    def send(self, notification: NotificationDTO) -> None:
        self.messages.append(notification)


def test_completed_run_creates_one_durable_notification(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    command = ChannelCommandDTO(
        "slack", "U1", "notification-1", "run example-workflow request=x", "C1"
    )
    response = controller.handle(command)
    run_id = response.text.split()[-1]
    controller.engine.run_until_idle(RunId(run_id))
    sender = FakeSender()
    notifier = NotificationService(SQLiteStore(tmp_path / "state.db"), {"slack": sender})
    assert notifier.deliver_pending() == 1
    assert notifier.deliver_pending() == 0
    assert sender.messages[0].channel_id == "C1"
    assert "실행 완료" in sender.messages[0].text
    status = controller.handle(ChannelCommandDTO("slack", "U1", "status-1", f"status {run_id}"))
    assert "결과 요약:" in status.text
    assert "plan:" in status.text


def test_only_initiating_actor_can_inspect_or_respond(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    source = tmp_path / "definitions" / "approval.yaml"
    source.write_text(
        """version: 1
id: approval
entry: ask
states:
  ask:
    kind: wait
    prompt: Approve?
    outcomes: [yes]
    on: {yes: done}
  done: {kind: end, result: success}
""",
        encoding="utf-8",
    )
    response = controller.handle(ChannelCommandDTO("slack", "U1", "start", "run approval", "C1"))
    run_id = RunId(response.text.split()[-1])
    controller.engine.run_until_idle(run_id)
    token_id = controller.engine.store.waiting_tokens(run_id)[0].id.value
    other_status = controller.handle(
        ChannelCommandDTO("slack", "U2", "read", f"status {run_id.value}")
    )
    other_answer = controller.handle(
        ChannelCommandDTO("slack", "U2", "answer", f"respond {token_id} yes")
    )
    assert "권한" in other_status.text
    assert "권한" in other_answer.text
    assert controller.engine.status(run_id).status == "waiting_input"
    owner_answer = controller.handle(
        ChannelCommandDTO("slack", "U1", "answer", f"respond {token_id} yes")
    )
    assert "저장" in owner_answer.text


def test_binding_after_wait_delivers_pending_prompt(tmp_path: Path) -> None:
    source = tmp_path / "approval.yaml"
    source.write_text(
        """version: 1
id: approval
entry: ask
states:
  ask:
    kind: wait
    prompt: 승인할까요?
    outcomes: [yes]
    on: {yes: done}
  done: {kind: end, result: success}
""",
        encoding="utf-8",
    )
    engine = build_engine(tmp_path / "state.db", tmp_path)
    definition = parse_definition(source)
    run_id = engine.start(definition, StartRunCommand(definition.id, ()))
    assert engine.run_until_idle(run_id).status == "waiting_input"
    store = SQLiteStore(tmp_path / "state.db")
    store.bind_channel(run_id, "discord", "D1", "C2")
    sender = FakeSender()
    assert NotificationService(store, {"discord": sender}).deliver_pending() == 1
    assert "승인할까요?" in sender.messages[0].text
