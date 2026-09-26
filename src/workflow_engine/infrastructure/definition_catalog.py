import re
from pathlib import Path

from workflow_engine.domain.model import Definition
from workflow_engine.infrastructure.yaml_definition import parse_definition


class FilesystemDefinitionCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def get(self, definition_id: str) -> Definition:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", definition_id):
            raise ValueError("invalid definition id")
        path = self.root / f"{definition_id}.yaml"
        if not path.resolve().is_relative_to(self.root) or not path.is_file():
            raise ValueError("definition is unavailable")
        definition = parse_definition(path)
        if definition.id != definition_id:
            raise ValueError("definition id does not match filename")
        return definition
