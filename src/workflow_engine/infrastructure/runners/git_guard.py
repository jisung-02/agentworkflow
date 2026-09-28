"""Reject QA approval when a runner changes the target Git checkout."""

import hashlib
import os
import subprocess
from pathlib import Path

from workflow_engine.application.dto import TaskRequestDTO, TaskResultDTO
from workflow_engine.application.ports import Runner


def _git(workdir: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(workdir), *args],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if completed.returncode:
        raise RuntimeError(f"QA Git inspection failed: {completed.stderr.decode(errors='replace')}")
    return completed.stdout


def _fingerprint(path: Path) -> str:
    if path.is_symlink():
        return f"link:{os.readlink(path)}"
    if not path.is_file():
        return "missing-or-directory"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(workdir: Path) -> tuple[str, tuple[tuple[str, str], ...]]:
    root = Path(os.fsdecode(_git(workdir, "rev-parse", "--show-toplevel").strip())).resolve()
    tracked = hashlib.sha256(
        _git(root, "diff", "--no-ext-diff", "--binary", "HEAD", "--")
    ).hexdigest()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    files = tuple(
        (os.fsdecode(relative), _fingerprint(root / os.fsdecode(relative)))
        for relative in untracked.split(b"\0")
        if relative
    )
    return tracked, files


class GitGuardRunner:
    def __init__(self, inner: Runner) -> None:
        self.inner = inner

    def run(self, request: TaskRequestDTO) -> TaskResultDTO:
        if request.branch_id is not None:
            raise ValueError("guarded QA cannot run inside a fork branch")
        workdir = Path(request.workdir).resolve()
        before = _snapshot(workdir)
        result = self.inner.run(request)
        after = _snapshot(workdir)
        if before != after:
            if "blocked" not in request.outcomes:
                raise RuntimeError("QA changed the Git checkout but no blocked outcome is declared")
            return TaskResultDTO(
                outcome="blocked",
                output=(
                    "QA 실행 중 Git 작업 트리의 파일 내용이 변경되었습니다. "
                    "변경 사항을 확인한 뒤 다시 검토하세요.\n\n" + result.output
                ),
                external_id=result.external_id,
            )
        return result
