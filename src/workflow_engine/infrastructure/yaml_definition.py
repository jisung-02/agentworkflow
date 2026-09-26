"""Strict YAML parsing and graph validation."""

from pathlib import Path

from workflow_engine.domain.model import Definition, State, TimerTrigger
from workflow_engine.infrastructure.unsafe_boundary import load_yaml


class DefinitionError(ValueError):
    """The workflow definition cannot be executed safely."""


def _mapping(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DefinitionError(f"{where} must be a mapping with string keys")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError(f"{where} must be a non-empty string")
    return value


def _allowed(data: dict[str, object], allowed: set[str], where: str) -> None:
    extra = data.keys() - allowed
    if extra:
        raise DefinitionError(f"{where} has unknown fields: {', '.join(sorted(extra))}")


def _pairs(value: object, where: str) -> tuple[tuple[str, str], ...]:
    data = _mapping(value, where)
    return tuple((key, _string(item, f"{where}.{key}")) for key, item in data.items())


def _outcomes(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DefinitionError(f"{where} must be a list")
    result = tuple(_string(item, where) for item in value)
    if not result or len(set(result)) != len(result):
        raise DefinitionError(f"{where} must contain unique outcomes")
    return result


def _instruction(value: object, root: Path, where: str) -> str:
    relative = Path(_string(value, where))
    if relative.is_absolute():
        raise DefinitionError(f"{where} must be relative")
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise DefinitionError(f"{where} must reference a file inside the definition directory")
    return resolved.read_text(encoding="utf-8")


def _state(state_id: str, value: object, root: Path) -> State:
    data = _mapping(value, f"states.{state_id}")
    kind = _string(data.get("kind"), f"states.{state_id}.kind")
    where = f"states.{state_id}"
    visits_value = data.get("max_visits")
    visits: int | None = None
    if visits_value is not None:
        if type(visits_value) is not int or visits_value < 1:
            raise DefinitionError(f"{where}.max_visits must be a positive integer")
        visits = visits_value
    if kind == "task":
        _allowed(
            data,
            {"kind", "runner", "instructions", "output", "outcomes", "on", "max_visits"},
            where,
        )
        outcomes = _outcomes(data.get("outcomes"), f"{where}.outcomes")
        on = _pairs(data.get("on"), f"{where}.on")
        if set(dict(on)) != set(outcomes):
            raise DefinitionError(f"{where}.on must cover every declared outcome")
        output = data.get("output")
        return State(
            id=state_id,
            kind="task",
            runner=_string(data.get("runner"), f"{where}.runner"),
            instructions=_instruction(data.get("instructions"), root, f"{where}.instructions"),
            output=_string(output, f"{where}.output") if output is not None else None,
            outcomes=outcomes,
            on=on,
            max_visits=visits,
        )
    if kind == "fork":
        _allowed(data, {"kind", "branches", "join", "max_visits"}, where)
        branches = _pairs(data.get("branches"), f"{where}.branches")
        if len(branches) < 2:
            raise DefinitionError(f"{where}.branches requires at least two branches")
        return State(
            id=state_id,
            kind="fork",
            branches=branches,
            join=_string(data.get("join"), f"{where}.join"),
            max_visits=visits,
        )
    if kind == "join":
        _allowed(data, {"kind", "policy", "on", "max_visits"}, where)
        if data.get("policy") != "all_success":
            raise DefinitionError(f"{where}.policy must be all_success")
        on = _pairs(data.get("on"), f"{where}.on")
        if set(dict(on)) != {"completed", "failed"}:
            raise DefinitionError(f"{where}.on must cover completed and failed")
        return State(id=state_id, kind="join", policy="all_success", on=on, max_visits=visits)
    if kind == "wait":
        _allowed(data, {"kind", "prompt", "outcomes", "on", "max_visits"}, where)
        outcomes = _outcomes(data.get("outcomes"), f"{where}.outcomes")
        on = _pairs(data.get("on"), f"{where}.on")
        if set(dict(on)) != set(outcomes):
            raise DefinitionError(f"{where}.on must cover every declared outcome")
        return State(
            id=state_id,
            kind="wait",
            prompt=_string(data.get("prompt"), f"{where}.prompt"),
            outcomes=outcomes,
            on=on,
            max_visits=visits,
        )
    if kind == "end":
        _allowed(data, {"kind", "result"}, where)
        result = data.get("result")
        if result not in ("success", "failure"):
            raise DefinitionError(f"{where}.result must be success or failure")
        return State(id=state_id, kind="end", result=_string(result, f"{where}.result"))
    raise DefinitionError(f"{where}.kind is unknown: {kind}")


def _timers(value: object) -> tuple[TimerTrigger, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DefinitionError("triggers must be a list")
    result: list[TimerTrigger] = []
    for index, item in enumerate(value):
        data = _mapping(item, f"triggers[{index}]")
        trigger_type = data.get("type")
        if trigger_type == "manual":
            _allowed(data, {"type"}, f"triggers[{index}]")
        elif trigger_type == "timer":
            _allowed(data, {"type", "cron", "timezone", "input"}, f"triggers[{index}]")
            result.append(
                TimerTrigger(
                    cron=_string(data.get("cron"), f"triggers[{index}].cron"),
                    timezone=_string(data.get("timezone"), f"triggers[{index}].timezone"),
                    inputs=_pairs(data.get("input", {}), f"triggers[{index}].input"),
                )
            )
        else:
            raise DefinitionError(f"triggers[{index}].type is unknown")
    return tuple(result)


def parse_definition(path: Path) -> Definition:
    data = _mapping(load_yaml(path.read_text(encoding="utf-8")), "definition")
    _allowed(data, {"version", "id", "name", "entry", "inputs", "triggers", "states"}, "definition")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise DefinitionError("version must be 1")
    input_data = _mapping(data.get("inputs", {}), "inputs")
    inputs: list[str] = []
    for name, raw in input_data.items():
        spec = _mapping(raw, f"inputs.{name}")
        _allowed(spec, {"type", "required"}, f"inputs.{name}")
        if spec.get("type") != "string" or spec.get("required") not in (True, "true"):
            raise DefinitionError(f"inputs.{name} supports only required strings in v1")
        inputs.append(name)
    states_data = _mapping(data.get("states"), "states")
    if not states_data:
        raise DefinitionError("states must not be empty")
    definition = Definition(
        id=_string(data.get("id"), "id"),
        name=_string(data.get("name", data.get("id")), "name"),
        entry=_string(data.get("entry"), "entry"),
        inputs=tuple(inputs),
        states=tuple(_state(name, raw, path.parent) for name, raw in states_data.items()),
        timers=_timers(data.get("triggers")),
    )
    validate_graph(definition)
    return definition


def validate_graph(definition: Definition) -> None:
    states = {state.id: state for state in definition.states}
    if definition.entry not in states:
        raise DefinitionError("entry must reference a state")
    edges: dict[str, tuple[str, ...]] = {}
    joins: set[str] = set()
    for state in definition.states:
        targets = tuple(target for _, target in state.on) + tuple(
            target for _, target in state.branches
        )
        for target in targets:
            if target not in states:
                raise DefinitionError(f"{state.id} references unknown state {target}")
        edges[state.id] = targets
        if state.kind == "fork":
            if state.join not in states or states[state.join].kind != "join":
                raise DefinitionError(f"{state.id}.join must reference a join state")
            if state.join in joins:
                raise DefinitionError(f"join {state.join} is shared by multiple forks")
            joins.add(state.join)
    for state in definition.states:
        if state.kind == "join" and state.id not in joins:
            raise DefinitionError(f"join {state.id} has no fork")
    reached: set[str] = set()
    active: list[str] = []

    def visit(node: str) -> None:
        if node in active:
            cycle = active[active.index(node) :]
            if not any(states[item].max_visits is not None for item in cycle):
                raise DefinitionError(f"unbounded cycle: {' -> '.join(cycle)}")
            return
        if node in reached:
            return
        reached.add(node)
        active.append(node)
        for target in edges[node]:
            visit(target)
        active.pop()

    visit(definition.entry)
    unused = states.keys() - reached
    if unused:
        raise DefinitionError(f"unreachable states: {', '.join(sorted(unused))}")
    for state in definition.states:
        if state.kind != "fork" or state.join is None:
            continue
        for branch, start in state.branches:
            _verify_branch(branch, start, state.join, states, edges, set())


def _verify_branch(
    branch: str,
    node: str,
    join: str,
    states: dict[str, State],
    edges: dict[str, tuple[str, ...]],
    active: set[str],
) -> None:
    if node == join:
        return
    if node in active or states[node].kind in ("fork", "join", "end"):
        raise DefinitionError(f"branch {branch} must reach only its declared join")
    targets = edges[node]
    if not targets:
        raise DefinitionError(f"branch {branch} does not reach join {join}")
    for target in targets:
        _verify_branch(branch, target, join, states, edges, active | {node})
