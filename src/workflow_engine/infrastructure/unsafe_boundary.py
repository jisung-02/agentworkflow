"""The only project module allowed to accept untyped external payloads."""

import json
from typing import Any

import yaml


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
