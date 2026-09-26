"""Versioned, typed persistence format for immutable definitions."""

import json
from dataclasses import asdict

from workflow_engine.domain.model import Definition, State, StateKind, TimerTrigger
from workflow_engine.infrastructure.unsafe_boundary import load_json
from workflow_engine.infrastructure.yaml_definition import DefinitionError, _mapping, _string


def definition_json(definition: Definition) -> str:
    return json.dumps(asdict(definition), ensure_ascii=False, sort_keys=True)


def _string_pairs(value: object, where: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise DefinitionError(f"{where} must be a list")
    pairs: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise DefinitionError(f"{where} contains an invalid pair")
        pairs.append((_string(item[0], where), _string(item[1], where)))
    return tuple(pairs)


def _optional_string(value: object, where: str) -> str | None:
    return None if value is None else _string(value, where)


def _state_kind(value: object) -> StateKind:
    if value == "task":
        return "task"
    if value == "fork":
        return "fork"
    if value == "join":
        return "join"
    if value == "wait":
        return "wait"
    if value == "end":
        return "end"
    raise DefinitionError("snapshot.state.kind is invalid")


def definition_from_json(payload: str) -> Definition:
    data = _mapping(load_json(payload), "snapshot")
    raw_states = data.get("states")
    raw_timers = data.get("timers")
    raw_inputs = data.get("inputs")
    if not isinstance(raw_states, list) or not isinstance(raw_timers, list):
        raise DefinitionError("snapshot must have states and timers")
    if not isinstance(raw_inputs, list):
        raise DefinitionError("snapshot must have inputs")
    states: list[State] = []
    for raw in raw_states:
        state = _mapping(raw, "snapshot.state")
        kind = _state_kind(state.get("kind"))
        visits = state.get("max_visits")
        if visits is not None and (type(visits) is not int or visits < 1):
            raise DefinitionError("snapshot.state.max_visits is invalid")
        raw_outcomes = state.get("outcomes")
        if not isinstance(raw_outcomes, list):
            raise DefinitionError("snapshot.state.outcomes is invalid")
        states.append(
            State(
                id=_string(state.get("id"), "snapshot.state.id"),
                kind=kind,
                on=_string_pairs(state.get("on"), "snapshot.state.on"),
                outcomes=tuple(_string(item, "outcome") for item in raw_outcomes),
                runner=_optional_string(state.get("runner"), "snapshot.state.runner"),
                instructions=_optional_string(
                    state.get("instructions"), "snapshot.state.instructions"
                ),
                output=_optional_string(state.get("output"), "snapshot.state.output"),
                branches=_string_pairs(state.get("branches"), "snapshot.state.branches"),
                join=_optional_string(state.get("join"), "snapshot.state.join"),
                policy=_optional_string(state.get("policy"), "snapshot.state.policy"),
                result=_optional_string(state.get("result"), "snapshot.state.result"),
                prompt=_optional_string(state.get("prompt"), "snapshot.state.prompt"),
                max_visits=visits,
            )
        )
    timers: list[TimerTrigger] = []
    for raw in raw_timers:
        timer = _mapping(raw, "snapshot.timer")
        timers.append(
            TimerTrigger(
                cron=_string(timer.get("cron"), "snapshot.timer.cron"),
                timezone=_string(timer.get("timezone"), "snapshot.timer.timezone"),
                inputs=_string_pairs(timer.get("inputs"), "snapshot.timer.inputs"),
            )
        )
    return Definition(
        id=_string(data.get("id"), "snapshot.id"),
        name=_string(data.get("name"), "snapshot.name"),
        entry=_string(data.get("entry"), "snapshot.entry"),
        inputs=tuple(_string(item, "snapshot.input") for item in raw_inputs),
        states=tuple(states),
        timers=tuple(timers),
    )
