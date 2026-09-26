import pytest

from workflow_engine.application.dto import TaskRequestDTO, TaskResultDTO
from workflow_engine.infrastructure import unsafe_boundary


class CustomRunner:
    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        return TaskResultDTO(request.outcomes[0], "custom")


class FakeEntry:
    name = "custom"

    def load(self) -> type[CustomRunner]:
        return CustomRunner


def test_entry_point_factory_matches_consumer_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unsafe_boundary, "entry_points", lambda *, group: (FakeEntry(),))
    assert isinstance(unsafe_boundary.load_runner_plugins()["custom"], CustomRunner)
