# Workflow Engine Local

YAML로 정의하는 로컬 상태 전이 워크플로 엔진. 현재 구현 진행 상태는 [PLAN.md](PLAN.md), 목표 명세는 [SPEC.md](SPEC.md)를 참고하세요.

개발 환경:

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```
