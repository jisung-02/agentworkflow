"""Channel-neutral commands consumed by Slack, Discord, and host skills."""

import shlex

from workflow_engine.application.dto import ChannelCommandDTO, ChannelResponseDTO, StartRunCommand
from workflow_engine.application.engine import WorkflowEngine
from workflow_engine.application.ports import ActorPolicy, DefinitionCatalog, NotificationStore
from workflow_engine.domain.value_objects import RunId, TokenId


class ChannelController:
    def __init__(
        self,
        engine: WorkflowEngine,
        catalog: DefinitionCatalog,
        policy: ActorPolicy,
        notifications: NotificationStore,
    ) -> None:
        self.engine = engine
        self.catalog = catalog
        self.policy = policy
        self.notifications = notifications

    def handle(self, command: ChannelCommandDTO) -> ChannelResponseDTO:
        if not self.policy.permits(command):
            return ChannelResponseDTO("이 계정에는 워크플로 명령 권한이 없습니다.")
        try:
            parts = shlex.split(command.text)
            if not parts:
                raise ValueError("명령이 비어 있습니다")
            action = parts[0]
            if action == "run" and len(parts) >= 2:
                definition = self.catalog.get(parts[1])
                inputs: dict[str, str] = {}
                for item in parts[2:]:
                    if "=" not in item:
                        raise ValueError("입력은 KEY=VALUE 형식이어야 합니다")
                    key, value = item.split("=", 1)
                    if not key or key in inputs:
                        raise ValueError("입력 이름이 비어 있거나 중복되었습니다")
                    inputs[key] = value
                run_id = self.engine.start(
                    definition,
                    StartRunCommand(
                        definition.id,
                        tuple(inputs.items()),
                        f"{command.source}:{command.message_id}",
                    ),
                )
                if command.channel_id is not None:
                    self.notifications.bind_channel(run_id, command.source, command.channel_id)
                return ChannelResponseDTO(f"실행 등록: {run_id.value}")
            if action == "status" and len(parts) == 2:
                run_id = RunId(parts[1])
                status = self.engine.status(run_id)
                waiting = self.engine.store.waiting_tokens(run_id)
                definition = self.engine.store.definition_for_run(run_id)
                pending = ", ".join(
                    f"{item.id.value}({definition.state(item.state_id).prompt})" for item in waiting
                )
                return ChannelResponseDTO(
                    f"{run_id.value}: {status.status}; 현재 상태: "
                    f"{', '.join(status.active_states) or '없음'}; 응답 대기: {pending or '없음'}"
                )
            if action == "respond" and len(parts) == 3:
                token_id = TokenId(parts[1])
                applied = self.engine.submit_input(token_id, parts[2])
                return ChannelResponseDTO(
                    "응답이 저장되었습니다." if applied else "이미 처리된 응답입니다."
                )
            raise ValueError(
                "run ID KEY=VALUE, status RUN_ID, respond TOKEN_ID OUTCOME 중 하나를 사용하세요"
            )
        except (ValueError, OSError) as exc:
            return ChannelResponseDTO(f"오류: {exc}")
