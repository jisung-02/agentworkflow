# Workflow Engine Local

YAML로 정의하는 로컬 상태 전이 워크플로 엔진. 실행 이력은 SQLite에 저장합니다. 현재 구현 진행 상태는 [PLAN.md](PLAN.md), 목표 명세는 [SPEC.md](SPEC.md)를 참고하세요.

## 설치와 실행

```bash
uv sync --dev
uv run workflow validate examples/example-workflow.yaml
uv run workflow graph examples/example-workflow.yaml
uv run workflow run examples/example-workflow.yaml --input request="새 기능 만들기"
```

`workflow run`은 JSON으로 실행 ID와 결과를 출력합니다. 기본 DB는 현재 디렉터리의 `.workflow/state.db`이며 `--db`로 바꿀 수 있습니다. `status RUN_ID`, `resume RUN_ID`로 조회·재개합니다. 사람의 응답을 기다리는 State는 `status`의 `waiting_tokens`에 토큰 ID가 표시되며 `respond TOKEN_ID OUTCOME`으로 제출합니다.

타이머가 있는 YAML은 `workflow register FILE`로 등록합니다. `workflow tick`은 기한이 된 스케줄을 한 번 실행하고, `workflow serve --interval 30`은 계속 폴링합니다. 해당 프로세스가 실행 중이어야 예약과 사용량 제한 후 재개가 진행됩니다.

`echo` runner는 설치 확인과 테스트용입니다. `codex` runner는 로컬 Codex CLI 로그인 상태를 이용해 `codex exec`를 호출합니다. 처음 사용하기 전에 `codex login`을 완료해야 합니다. 병렬 branch의 코드 수정은 격리된 worktree 통합이 완료될 때까지 지원하지 않습니다.

개발 환경:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
uv build
```
