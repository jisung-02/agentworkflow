from pathlib import Path

import pytest

from workflow_engine.infrastructure.yaml_definition import DefinitionError, parse_definition

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "example-workflow.yaml"


def test_example_graph_has_parallel_branches() -> None:
    definition = parse_definition(EXAMPLE)
    assert definition.entry == "plan"
    assert definition.state("parallel").branches == (
        ("implementation", "implement"),
        ("documentation", "document"),
    )
    assert definition.state("plan").instructions is not None


def test_rejects_branch_that_skips_join(tmp_path: Path) -> None:
    source = EXAMPLE.read_text(encoding="utf-8")
    (tmp_path / "instructions").mkdir()
    for name in ("plan", "implement", "document"):
        (tmp_path / "instructions" / f"{name}.md").write_text(name, encoding="utf-8")
    path = tmp_path / "bad.yaml"
    path.write_text(source.replace("completed: gather", "completed: done", 1), encoding="utf-8")
    with pytest.raises(DefinitionError, match="declared join"):
        parse_definition(path)


def test_rejects_unbounded_cycle(tmp_path: Path) -> None:
    path = tmp_path / "cycle.yaml"
    path.write_text(
        """version: 1
id: cycle
entry: first
states:
  first:
    kind: wait
    prompt: Continue?
    outcomes: [yes]
    on: {yes: first}
""",
        encoding="utf-8",
    )
    with pytest.raises(DefinitionError, match="unbounded cycle"):
        parse_definition(path)


def test_rejects_instruction_escape(tmp_path: Path) -> None:
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    folder = tmp_path / "workflow"
    folder.mkdir()
    path = folder / "bad.yaml"
    path.write_text(
        """version: 1
id: bad
entry: first
states:
  first:
    kind: task
    runner: echo
    instructions: ../outside.md
    outcomes: [completed]
    on: {completed: last}
  last: {kind: end, result: success}
""",
        encoding="utf-8",
    )
    with pytest.raises(DefinitionError, match="inside"):
        parse_definition(path)
