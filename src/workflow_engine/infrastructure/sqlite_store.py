"""SQLite implementation of the workflow persistence port."""

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from workflow_engine.application.dto import RunStatus, RunStatusDTO, TokenDTO
from workflow_engine.domain.errors import VisitLimitError
from workflow_engine.domain.model import Definition
from workflow_engine.domain.value_objects import RunId, TokenId
from workflow_engine.infrastructure.serialization import definition_from_json, definition_json
from workflow_engine.infrastructure.unsafe_boundary import load_json
from workflow_engine.infrastructure.yaml_definition import _mapping, _string


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS definitions (
                    hash TEXT PRIMARY KEY, definition_id TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, definition_hash TEXT NOT NULL REFERENCES definitions(hash),
                    status TEXT NOT NULL, inputs TEXT NOT NULL, outputs TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    state_id TEXT NOT NULL, status TEXT NOT NULL, fork_id TEXT,
                    branch_id TEXT, last_outcome TEXT, wake_at REAL,
                    claimed_at REAL, version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS forks (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    join_state TEXT NOT NULL, expected INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS join_arrivals (
                    fork_id TEXT NOT NULL REFERENCES forks(id), branch_id TEXT NOT NULL,
                    outcome TEXT NOT NULL, PRIMARY KEY (fork_id, branch_id)
                );
                CREATE TABLE IF NOT EXISTS visits (
                    run_id TEXT NOT NULL REFERENCES runs(id), state_id TEXT NOT NULL,
                    count INTEGER NOT NULL, PRIMARY KEY (run_id, state_id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    token_id TEXT NOT NULL REFERENCES tokens(id), number INTEGER NOT NULL,
                    status TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,
                    PRIMARY KEY (token_id, number)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    token_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tokens_ready ON tokens(run_id, status);
                CREATE INDEX IF NOT EXISTS tokens_wake ON tokens(status, wake_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _event(
        conn: sqlite3.Connection, run_id: str, token_id: str | None, kind: str, detail: str
    ) -> None:
        conn.execute(
            "INSERT INTO events(run_id, token_id, kind, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (run_id, token_id, kind, detail, time.time()),
        )

    @staticmethod
    def _visit(conn: sqlite3.Connection, run_id: str, state_id: str, limit: int | None) -> None:
        row = conn.execute(
            "SELECT count FROM visits WHERE run_id = ? AND state_id = ?", (run_id, state_id)
        ).fetchone()
        count = (int(row["count"]) if row else 0) + 1
        if limit is not None and count > limit:
            raise VisitLimitError(f"state {state_id} exceeded max_visits={limit}")
        conn.execute(
            "INSERT INTO visits(run_id, state_id, count) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id, state_id) DO UPDATE SET count=excluded.count",
            (run_id, state_id, count),
        )

    @staticmethod
    def _token(row: sqlite3.Row) -> TokenDTO:
        return TokenDTO(
            id=TokenId(str(row["id"])),
            run_id=RunId(str(row["run_id"])),
            state_id=str(row["state_id"]),
            status=str(row["status"]),
            fork_id=str(row["fork_id"]) if row["fork_id"] is not None else None,
            branch_id=str(row["branch_id"]) if row["branch_id"] is not None else None,
            last_outcome=str(row["last_outcome"]) if row["last_outcome"] is not None else None,
            version=int(row["version"]),
        )

    @staticmethod
    def _pairs(payload: str) -> tuple[tuple[str, str], ...]:
        data = _mapping(load_json(payload), "stored values")
        return tuple((key, _string(value, key)) for key, value in data.items())

    def save_definition(self, definition: Definition) -> str:
        payload = definition_json(definition)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO definitions(hash, definition_id, payload) VALUES (?, ?, ?)",
                (digest, definition.id, payload),
            )
        return digest

    def create_run(
        self, definition_hash: str, inputs: tuple[tuple[str, str], ...], key: str | None = None
    ) -> RunId:
        with self._transaction() as conn:
            if key is not None:
                existing = conn.execute(
                    "SELECT id, definition_hash, inputs FROM runs WHERE idempotency_key = ?", (key,)
                ).fetchone()
                if existing is not None:
                    if (
                        existing["definition_hash"] != definition_hash
                        or self._pairs(existing["inputs"]) != inputs
                    ):
                        raise ValueError("idempotency key was used with different inputs")
                    return RunId(str(existing["id"]))
            row = conn.execute(
                "SELECT payload FROM definitions WHERE hash = ?", (definition_hash,)
            ).fetchone()
            if row is None:
                raise ValueError("definition snapshot is unknown")
            definition = definition_from_json(str(row["payload"]))
            run_id = RunId(uuid.uuid4().hex)
            token_id = TokenId(uuid.uuid4().hex)
            conn.execute(
                "INSERT INTO runs(id, definition_hash, status, inputs, outputs, "
                "idempotency_key, created_at) "
                "VALUES (?, ?, 'running', ?, '{}', ?, ?)",
                (run_id.value, definition_hash, json.dumps(dict(inputs)), key, time.time()),
            )
            conn.execute(
                "INSERT INTO tokens(id, run_id, state_id, status) VALUES (?, ?, ?, 'ready')",
                (token_id.value, run_id.value, definition.entry),
            )
            self._visit(
                conn, run_id.value, definition.entry, definition.state(definition.entry).max_visits
            )
            self._event(conn, run_id.value, token_id.value, "run_started", definition.entry)
            return run_id

    def definition_for_run(self, run_id: RunId) -> Definition:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT d.payload FROM runs r JOIN definitions d "
                "ON r.definition_hash = d.hash WHERE r.id = ?",
                (run_id.value,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown run {run_id.value}")
        return definition_from_json(str(row["payload"]))

    def inputs_for_run(self, run_id: RunId) -> tuple[tuple[str, str], ...]:
        return self._run_pairs(run_id, "inputs")

    def outputs_for_run(self, run_id: RunId) -> tuple[tuple[str, str], ...]:
        return self._run_pairs(run_id, "outputs")

    def _run_pairs(self, run_id: RunId, column: str) -> tuple[tuple[str, str], ...]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT {column} FROM runs WHERE id = ?", (run_id.value,)
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown run {run_id.value}")
        return self._pairs(str(row[column]))

    def status(self, run_id: RunId) -> RunStatusDTO:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT r.status, r.outputs, d.definition_id FROM runs r "
                "JOIN definitions d ON r.definition_hash = d.hash WHERE r.id = ?",
                (run_id.value,),
            ).fetchone()
            tokens = conn.execute(
                "SELECT state_id, status FROM tokens WHERE run_id = ? "
                "AND status NOT IN ('done', 'joined')",
                (run_id.value,),
            ).fetchall()
        if row is None:
            raise ValueError(f"unknown run {run_id.value}")
        statuses = {str(token["status"]) for token in tokens}
        status: RunStatus = "running"
        if row["status"] == "success":
            status = "success"
        elif row["status"] == "failure":
            status = "failure"
        elif "needs_attention" in statuses:
            status = "needs_attention"
        elif "waiting_host" in statuses:
            status = "waiting_host"
        elif "waiting_quota" in statuses:
            status = "waiting_quota"
        elif "waiting_input" in statuses:
            status = "waiting_input"
        return RunStatusDTO(
            run_id=run_id,
            definition_id=str(row["definition_id"]),
            status=status,
            active_states=tuple(str(token["state_id"]) for token in tokens),
            outputs=self._pairs(str(row["outputs"])),
        )

    def next_ready(self, run_id: RunId) -> TokenDTO | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM tokens WHERE run_id = ? AND status = 'ready' ORDER BY rowid LIMIT 1",
                (run_id.value,),
            ).fetchone()
        return self._token(row) if row is not None else None

    def waiting_tokens(self, run_id: RunId) -> tuple[TokenDTO, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM tokens WHERE run_id=? AND status='waiting_input' ORDER BY rowid",
                (run_id.value,),
            ).fetchall()
        return tuple(self._token(row) for row in rows)

    def token(self, token_id: TokenId) -> TokenDTO:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM tokens WHERE id = ?", (token_id.value,)).fetchone()
        if row is None:
            raise ValueError(f"unknown token {token_id.value}")
        return self._token(row)

    def claim(self, token: TokenDTO) -> TokenDTO | None:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='executing', claimed_at=?, version=version+1 "
                "WHERE id=? AND state_id=? AND status='ready' AND version=?",
                (time.time(), token.id.value, token.state_id, token.version),
            ).rowcount
            if not changed:
                return None
            number = (
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM attempts WHERE token_id = ?", (token.id.value,)
                    ).fetchone()[0]
                )
                + 1
            )
            conn.execute(
                "INSERT INTO attempts(token_id, number, status, started_at) "
                "VALUES (?, ?, 'executing', ?)",
                (token.id.value, number, time.time()),
            )
            self._event(conn, token.run_id.value, token.id.value, "task_started", token.state_id)
            row = conn.execute("SELECT * FROM tokens WHERE id = ?", (token.id.value,)).fetchone()
            return self._token(row)

    def move(
        self,
        token: TokenDTO,
        outcome: str,
        target: str,
        max_visits: int | None,
        output_name: str | None = None,
        output: str = "",
    ) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET state_id=?, status='ready', last_outcome=?, claimed_at=NULL, "
                "version=version+1 WHERE id=? AND state_id=? AND status=? AND version=?",
                (target, outcome, token.id.value, token.state_id, token.status, token.version),
            ).rowcount
            if not changed:
                return False
            self._visit(conn, token.run_id.value, target, max_visits)
            if token.status == "executing":
                conn.execute(
                    "UPDATE attempts SET status='completed', ended_at=? "
                    "WHERE token_id=? AND number=("
                    "SELECT MAX(number) FROM attempts WHERE token_id=?)",
                    (time.time(), token.id.value, token.id.value),
                )
            if output_name is not None:
                row = conn.execute(
                    "SELECT outputs FROM runs WHERE id=?", (token.run_id.value,)
                ).fetchone()
                outputs = dict(self._pairs(str(row["outputs"])))
                outputs[output_name] = output
                conn.execute(
                    "UPDATE runs SET outputs=? WHERE id=?",
                    (json.dumps(outputs, ensure_ascii=False), token.run_id.value),
                )
            self._event(
                conn, token.run_id.value, token.id.value, "transition", f"{outcome}:{target}"
            )
            return True

    def fork(
        self,
        token: TokenDTO,
        join: str,
        branches: tuple[tuple[str, str], ...],
        limits: tuple[tuple[str, int | None], ...],
    ) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='done', version=version+1 WHERE id=? AND state_id=? "
                "AND status='ready' AND version=?",
                (token.id.value, token.state_id, token.version),
            ).rowcount
            if not changed:
                return False
            fork_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO forks(id, run_id, join_state, expected) VALUES (?, ?, ?, ?)",
                (fork_id, token.run_id.value, join, len(branches)),
            )
            limit_map = dict(limits)
            for branch_id, target in branches:
                child_id = uuid.uuid4().hex
                self._visit(conn, token.run_id.value, target, limit_map[target])
                conn.execute(
                    "INSERT INTO tokens(id, run_id, state_id, status, fork_id, branch_id) "
                    "VALUES (?, ?, ?, 'ready', ?, ?)",
                    (child_id, token.run_id.value, target, fork_id, branch_id),
                )
            self._event(conn, token.run_id.value, token.id.value, "fork", fork_id)
            return True

    def arrive_join(
        self,
        token: TokenDTO,
        success_target: str,
        failure_target: str,
        success_limit: int | None,
        failure_limit: int | None,
    ) -> bool:
        if token.fork_id is None or token.branch_id is None:
            raise ValueError("join requires a fork branch token")
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='joined', version=version+1 WHERE id=? AND state_id=? "
                "AND status='ready' AND version=?",
                (token.id.value, token.state_id, token.version),
            ).rowcount
            if not changed:
                return False
            conn.execute(
                "INSERT INTO join_arrivals(fork_id, branch_id, outcome) VALUES (?, ?, ?)",
                (token.fork_id, token.branch_id, token.last_outcome or "completed"),
            )
            fork = conn.execute(
                "SELECT expected, completed FROM forks WHERE id=? AND join_state=?",
                (token.fork_id, token.state_id),
            ).fetchone()
            if fork is None or int(fork["completed"]) != 0:
                raise ValueError("join does not match its fork")
            arrivals = conn.execute(
                "SELECT outcome FROM join_arrivals WHERE fork_id=?", (token.fork_id,)
            ).fetchall()
            if len(arrivals) == int(fork["expected"]):
                succeeded = all(str(item["outcome"]) == "completed" for item in arrivals)
                target = success_target if succeeded else failure_target
                limit = success_limit if succeeded else failure_limit
                self._visit(conn, token.run_id.value, target, limit)
                conn.execute("UPDATE forks SET completed=1 WHERE id=?", (token.fork_id,))
                conn.execute(
                    "INSERT INTO tokens(id, run_id, state_id, status, last_outcome) "
                    "VALUES (?, ?, ?, 'ready', ?)",
                    (
                        uuid.uuid4().hex,
                        token.run_id.value,
                        target,
                        "completed" if succeeded else "failed",
                    ),
                )
                self._event(conn, token.run_id.value, token.id.value, "join", target)
            else:
                self._event(
                    conn, token.run_id.value, token.id.value, "join_arrival", token.branch_id
                )
            return True

    def wait_for_input(self, token: TokenDTO) -> bool:
        with self._transaction() as conn:
            return bool(
                conn.execute(
                    "UPDATE tokens SET status='waiting_input', version=version+1 "
                    "WHERE id=? AND state_id=? "
                    "AND status='ready' AND version=?",
                    (token.id.value, token.state_id, token.version),
                ).rowcount
            )

    def submit_input(self, token: TokenDTO, outcome: str, target: str, limit: int | None) -> bool:
        if token.status != "waiting_input":
            return False
        return self.move(token, outcome, target, limit)

    def end(self, token: TokenDTO, result: str) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='done', version=version+1 WHERE id=? AND state_id=? "
                "AND status='ready' AND version=?",
                (token.id.value, token.state_id, token.version),
            ).rowcount
            if not changed:
                return False
            conn.execute("UPDATE runs SET status=? WHERE id=?", (result, token.run_id.value))
            self._event(conn, token.run_id.value, token.id.value, "run_ended", result)
            return True

    def quota_wait(self, token: TokenDTO, wake_at: float) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='waiting_quota', wake_at=?, version=version+1 "
                "WHERE id=? AND status='executing' AND version=?",
                (wake_at, token.id.value, token.version),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE attempts SET status='quota', ended_at=? WHERE token_id=? AND number=("
                    "SELECT MAX(number) FROM attempts WHERE token_id=?)",
                    (time.time(), token.id.value, token.id.value),
                )
                self._event(conn, token.run_id.value, token.id.value, "quota_wait", str(wake_at))
            return bool(changed)

    def wake_due(self, now: float) -> int:
        with self._transaction() as conn:
            return int(
                conn.execute(
                    "UPDATE tokens SET status='ready', wake_at=NULL, version=version+1 "
                    "WHERE status='waiting_quota' AND wake_at<=?",
                    (now,),
                ).rowcount
            )

    def mark_attention(self, token: TokenDTO) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE tokens SET status='needs_attention', version=version+1 "
                "WHERE id=? AND version=?",
                (token.id.value, token.version),
            )
            self._event(conn, token.run_id.value, token.id.value, "needs_attention", token.state_id)

    def recover_inflight(self, run_id: RunId) -> int:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='needs_attention', version=version+1 "
                "WHERE run_id=? AND status='executing'",
                (run_id.value,),
            ).rowcount
            if changed:
                self._event(conn, run_id.value, None, "recovered_inflight", str(changed))
            return int(changed)
