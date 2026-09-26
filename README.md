# Workflow Engine Local

YAML로 정의하는 로컬 상태 전이 워크플로 엔진. 실행 이력은 SQLite에 저장합니다. 현재 구현 진행 상태는 [PLAN.md](PLAN.md), 목표 명세는 [SPEC.md](SPEC.md)를 참고하세요.

## 설치와 실행

```bash
uv sync --dev
uv run workflow validate examples/example-workflow.yaml
uv run workflow graph examples/example-workflow.yaml
uv run workflow run examples/example-workflow.yaml --input request="새 기능 만들기"
```

`workflow run`은 JSON으로 실행 ID와 결과를 출력합니다. 기본 DB는 현재 디렉터리의 `.workflow/state.db`이며 `--db`로 바꿀 수 있습니다. `status RUN_ID`, `resume RUN_ID`로 조회·재개합니다. 사람의 응답을 기다리는 State는 `status`의 `waiting_tokens`에 토큰 ID가 표시되며 `respond TOKEN_ID OUTCOME`으로 제출합니다. Task 결과는 DB의 `outputs`와 DB 옆 `artifacts/`의 파일에 저장되며, `status`의 `artifacts`에서 경로와 SHA-256을 확인할 수 있습니다.

타이머가 있는 YAML은 `workflow register FILE`로 등록합니다. `workflow tick`은 기한이 된 스케줄을 한 번 실행하고, `workflow serve --interval 30`은 계속 폴링합니다. 해당 프로세스가 실행 중이어야 예약과 사용량 제한 후 재개가 진행됩니다. 워커는 만료된 실행 lease를 `needs_attention`으로 회수하고, 개별 실행 오류가 나도 다른 준비된 실행을 계속 처리합니다.

`echo` runner는 설치 확인과 테스트용입니다. `codex` runner는 로컬 Codex CLI 로그인 상태를 이용해 `codex exec`를 호출합니다. `codex-review`는 같은 CLI를 읽기 전용 sandbox에서 실행하는 검토용 runner입니다. 처음 사용하기 전에 `codex login`을 완료해야 합니다. 병렬 Codex branch는 원본 Git 저장소의 `HEAD`에서 각각 별도 worktree를 만들며, 원본의 미커밋 변경은 복사되지 않습니다. branch 결과에는 worktree 경로가 포함됩니다. join 뒤 변경 병합은 YAML에서 명시한 후속 작업이 수행해야 합니다.

Codex 사용량 제한이 발생하면 재설정 시각과 세션 ID를 저장하고 `workflow serve`가 재설정 후 같은 세션을 재개합니다. CLI가 세션 ID를 반환하지 않아 안전하게 재개할 수 없는 경우 Run은 `needs_attention`으로 남습니다. 실제 모델 호출은 자동 테스트에 포함되지 않습니다.

## Slack과 Discord

HTTP 채널은 선택 의존성입니다.

```bash
uv sync --all-extras --dev
export WORKFLOW_ALLOWED_ACTORS='slack:U123,discord:123456789'
export WORKFLOW_SLACK_SIGNING_SECRET='...'
export WORKFLOW_DISCORD_PUBLIC_KEY='...'
uv run --all-extras workflow serve-http --definitions ./definitions
```

`definitions/ID.yaml`에 정의 파일을 두고 그 안의 `id`를 `ID`와 맞춥니다. Slack slash command의 Request URL은 `/slack/command`, Discord interaction endpoint는 `/discord/interactions`입니다. Discord 애플리케이션 명령에는 `text` 문자열 옵션을 추가합니다. 두 채널 모두 `run ID request=...`, `status RUN_ID`, `respond TOKEN_ID OUTCOME`을 받습니다. 서명 검증과 허용 사용자 목록을 통과해야 하며, `status`와 `respond`는 해당 채널에서 실행을 시작한 계정만 사용할 수 있습니다. 기존 DB의 실행 중 소유자 정보가 없는 것은 채널에서 조회할 수 없으므로 CLI로 확인하거나 새 실행을 시작하세요. 작업은 HTTP 요청에서 시작만 하고 별도의 `workflow serve` 프로세스가 실행합니다.

사람의 응답 대기 프롬프트와 실행 완료 알림은 SQLite outbox에 저장됩니다. `workflow serve`에 `WORKFLOW_SLACK_BOT_TOKEN` 또는 `WORKFLOW_DISCORD_BOT_TOKEN`을 설정하면 원래 명령을 보낸 채널로 메시지를 전송합니다. 토큰이 없거나 전송에 실패하면 알림은 대기 상태로 남아 다음 폴링에서 재시도합니다. 외부 전송 직후 프로세스가 중단되면 같은 알림이 중복 전달될 수 있습니다. 봇에는 대상 채널에 메시지를 보낼 권한이 필요합니다.

## Codex·Claude Code 호스트 스킬

`runner: codex-host` 또는 `runner: claude-host`인 State는 해당 호스트가 작업을 가져갈 때까지 `waiting_host`로 남습니다. 타이머로 만든 Run도 동일하게 대기합니다.

```bash
workflow host-next --runner claude-host
workflow host-heartbeat TOKEN_ID
workflow host-complete TOKEN_ID approved --output-file result.txt
```

Codex 스킬 파일은 [`integrations/codex/workflow-host`](integrations/codex/workflow-host), Claude Code 플러그인은 [`integrations/claude`](integrations/claude)에 있습니다. Codex에서는 스킬 폴더를 `~/.codex/skills/`에 복사하거나 연결합니다. Claude Code에서는 저장소를 받은 뒤 `claude --plugin-dir ./integrations/claude`로 플러그인을 로드할 수 있습니다. 양쪽 모두 `workflow` CLI가 설치되어 있고 같은 DB 경로를 사용해야 합니다. 스킬은 이미 로그인한 호스트 안에서 작업하며 별도의 API 키를 요구하지 않습니다.

GitHub에서 CLI를 설치하려면 `uv tool install git+https://github.com/jisung-02/agentworkflow.git`을 사용할 수 있습니다. PyPI 공개는 아직 진행하지 않았습니다.

## 사용자 정의 runner

별도 Python 패키지의 entry point 그룹 `workflow_engine.runners`에 `이름 = "패키지:팩토리"`를 등록합니다. 팩토리는 인자 없이 호출되며 `run(TaskRequestDTO) -> TaskResultDTO`를 구현한 객체를 반환해야 합니다. YAML의 `runner`에 해당 이름을 적습니다. 설치된 runner는 Composition Root에서 찾아 `Runner` 인터페이스에 주입하며, `echo`, `codex`, `codex-review`, `claude-host`, `codex-host`는 예약된 이름입니다. 외부 패키지 로딩 중 생기는 동적 타입은 `unsafe_boundary.py`에서 검증합니다.

개발 환경:

```bash
uv run --all-extras ruff check .
uv run --all-extras ruff format --check .
uv run --all-extras ty check
uv run --all-extras pytest
uv build
```
