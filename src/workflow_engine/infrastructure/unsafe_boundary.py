"""The only project module allowed to accept untyped external payloads."""

import json
from importlib.metadata import entry_points
from typing import Any

import yaml

from workflow_engine.application.ports import Runner


class _WorkflowLoader(yaml.SafeLoader):
    """Safe YAML loader with YAML 1.2-like string handling for on/yes/no."""


_WorkflowLoader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_yaml(text: str) -> object:
    value: Any = yaml.load(text, Loader=_WorkflowLoader)
    return value


def load_json(text: str) -> object:
    value: Any = json.loads(text)
    return value


def load_runner_plugins() -> dict[str, Runner]:
    """Validate dynamically loaded package entry points before crossing the port boundary."""
    runners: dict[str, Runner] = {}
    for entry in entry_points(group="workflow_engine.runners"):
        factory: Any = entry.load()
        if not callable(factory):
            raise TypeError(f"runner entry point {entry.name} is not a factory")
        instance: Any = factory()
        if not isinstance(instance, Runner):
            raise TypeError(f"runner entry point {entry.name} does not implement Runner")
        if entry.name in runners:
            raise ValueError(f"duplicate runner entry point: {entry.name}")
        runners[entry.name] = instance
    return runners
