"""Content-addressed local text artifacts."""

import hashlib
import os
import tempfile
from pathlib import Path

from workflow_engine.domain.value_objects import ArtifactRef


class FileArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_text(self, text: str) -> ArtifactRef:
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        target = self.root / f"{digest}.txt"
        if not target.is_file():
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as temp:
                temp.write(data)
                temporary = Path(temp.name)
            try:
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return ArtifactRef(digest=digest, path=str(target))
