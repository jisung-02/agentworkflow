"""SQLite implementation of the workflow persistence port."""

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from workflow_engine.application.dto import (
    NotificationDTO,
    RunStatus,
    RunStatusDTO,
    ScheduleDTO,
    TaskResultDTO,
    TokenDTO,
)
from workflow_engine.domain.errors import VisitLimitError
from workflow_engine.domain.model import Definition, TimerTrigger
from workflow_engine.domain.value_objects import ArtifactRef, RunId, TokenId
from workflow_engine.infrastructure.serialization import definition_from_json, definition_json
from workflow_engine.infrastructure.unsafe_boundary import load_json
from workflow_engine.infrastructure.yaml_definition import _mapping, _string


class SQLiteStore:
    LEASE_SECONDS = 7200

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
                    claimed_at REAL, lease_until REAL, version INTEGER NOT NULL DEFAULT 0,
                    external_id TEXT, attention_reason TEXT, attention_detail TEXT
                );
                CREATE TABLE IF NOT EXISTS forks (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    join_state TEXT NOT NULL, expected INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0, base_outputs TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS branch_outputs (
                    fork_id TEXT NOT NULL REFERENCES forks(id), branch_id TEXT NOT NULL,
                    output_name TEXT NOT NULL, output TEXT NOT NULL,
                    PRIMARY KEY (fork_id, branch_id, output_name)
                );
                CREATE TABLE IF NOT EXISTS pending_task_results (
                    token_id TEXT PRIMARY KEY REFERENCES tokens(id),
                    outcome TEXT NOT NULL, output TEXT NOT NULL, external_id TEXT
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
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY, definition_hash TEXT NOT NULL REFERENCES definitions(hash),
                    cron TEXT NOT NULL, timezone TEXT NOT NULL, inputs TEXT NOT NULL,
                    next_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    run_id TEXT NOT NULL REFERENCES runs(id), source TEXT NOT NULL,
                    channel_id TEXT NOT NULL, PRIMARY KEY (run_id, source, channel_id)
                );
                CREATE TABLE IF NOT EXISTS run_actors (
                    run_id TEXT NOT NULL REFERENCES runs(id), source TEXT NOT NULL,
                    actor_id TEXT NOT NULL, PRIMARY KEY (run_id, source)
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    source TEXT NOT NULL, channel_id TEXT NOT NULL,
                    text TEXT NOT NULL, delivery_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    run_id TEXT NOT NULL REFERENCES runs(id), output_name TEXT NOT NULL,
                    digest TEXT NOT NULL, path TEXT NOT NULL,
                    PRIMARY KEY (run_id, output_name)
                );
                CREATE INDEX IF NOT EXISTS tokens_ready ON tokens(run_id, status);
                CREATE INDEX IF NOT EXISTS tokens_wake ON tokens(status, wake_at);
                CREATE INDEX IF NOT EXISTS schedules_due ON schedules(next_at);
                CREATE INDEX IF NOT EXISTS notifications_pending ON notifications(status);
                """
            )
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(tokens)")}
            if "external_id" not in columns:
                conn.execute("ALTER TABLE tokens ADD COLUMN external_id TEXT")
            if "lease_until" not in columns:
                conn.execute("ALTER TABLE tokens ADD COLUMN lease_until REAL")
            if "attention_reason" not in columns:
                conn.execute("ALTER TABLE tokens ADD COLUMN attention_reason TEXT")
            if "attention_detail" not in columns:
                conn.execute("ALTER TABLE tokens ADD COLUMN attention_detail TEXT")
            fork_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(forks)")}
            if "base_outputs" not in fork_columns:
                conn.execute("ALTER TABLE forks ADD COLUMN base_outputs TEXT NOT NULL DEFAULT '{}'")

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
    def _queue_notification(conn: sqlite3.Connection, run_id: str, message: str, key: str) -> None:
        bindings = conn.execute(
            "SELECT source, channel_id FROM channel_bindings WHERE run_id=?", (run_id,)
        ).fetchall()
        for binding in bindings:
            source = str(binding["source"])
            channel_id = str(binding["channel_id"])
            conn.execute(
                "INSERT OR IGNORE INTO notifications(id, run_id, source, channel_id, "
                "text, delivery_key) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    run_id,
                    source,
                    channel_id,
                    message,
                    f"{key}:{source}:{channel_id}",
                ),
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
            external_id=str(row["external_id"]) if row["external_id"] is not None else None,
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

    def outputs_for_token(self, token: TokenDTO) -> tuple[tuple[str, str], ...]:
        if token.fork_id is None or token.branch_id is None:
            return self.outputs_for_run(token.run_id)
        with closing(self._connect()) as conn:
            fork = conn.execute(
                "SELECT base_outputs FROM forks WHERE id=? AND run_id=?",
                (token.fork_id, token.run_id.value),
            ).fetchone()
            if fork is None:
                raise ValueError("token fork is unknown")
            outputs = dict(self._pairs(str(fork["base_outputs"])))
            rows = conn.execute(
                "SELECT output_name, output FROM branch_outputs WHERE fork_id=? AND branch_id=?",
                (token.fork_id, token.branch_id),
            ).fetchall()
            outputs.update((str(row["output_name"]), str(row["output"])) for row in rows)
            return tuple(outputs.items())

    def pending_task_result(self, token_id: TokenId) -> TaskResultDTO | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT outcome, output, external_id FROM pending_task_results WHERE token_id=?",
                (token_id.value,),
            ).fetchone()
        if row is None:
            return None
        return TaskResultDTO(
            str(row["outcome"]),
            str(row["output"]),
            str(row["external_id"]) if row["external_id"] is not None else None,
        )

    def stage_task_result(self, token: TokenDTO, result: TaskResultDTO) -> bool:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT status, version FROM tokens WHERE id=?", (token.id.value,)
            ).fetchone()
            if row is None or row["status"] != "executing" or row["version"] != token.version:
                return False
            existing = conn.execute(
                "SELECT outcome, output, external_id FROM pending_task_results WHERE token_id=?",
                (token.id.value,),
            ).fetchone()
            if existing is not None:
                return (
                    existing["outcome"] == result.outcome
                    and existing["output"] == result.output
                    and existing["external_id"] == result.external_id
                )
            conn.execute(
                "INSERT INTO pending_task_results(token_id, outcome, output, external_id) "
                "VALUES (?, ?, ?, ?)",
                (token.id.value, result.outcome, result.output, result.external_id),
            )
            return True

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
            artifacts = conn.execute(
                "SELECT output_name, digest, path FROM artifacts "
                "WHERE run_id=? ORDER BY output_name",
                (run_id.value,),
            ).fetchall()
            attention = conn.execute(
                "SELECT state_id, attention_reason, attention_detail FROM tokens "
                "WHERE run_id=? AND status='needs_attention' ORDER BY rowid",
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
            artifacts=tuple(
                (str(item["output_name"]), ArtifactRef(str(item["digest"]), str(item["path"])))
                for item in artifacts
            ),
            attention=tuple(
                (
                    str(item["state_id"]),
                    f"{item['attention_reason'] or 'unknown'}: "
                    f"{item['attention_detail'] or ''}".strip(),
                )
                for item in attention
            ),
        )

    def next_ready(self, run_id: RunId) -> TokenDTO | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM tokens WHERE run_id = ? AND status = 'ready' ORDER BY rowid LIMIT 1",
                (run_id.value,),
            ).fetchone()
        return self._token(row) if row is not None else None

    def ready_run_ids(self) -> tuple[RunId, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT run_id FROM tokens WHERE status='ready' ORDER BY run_id"
            ).fetchall()
        return tuple(RunId(str(row["run_id"])) for row in rows)

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
                "UPDATE tokens SET status='executing', claimed_at=?, lease_until=?, "
                "version=version+1 "
                "WHERE id=? AND state_id=? AND status='ready' AND version=?",
                (
                    time.time(),
                    time.time() + self.LEASE_SECONDS,
                    token.id.value,
                    token.state_id,
                    token.version,
                ),
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
        artifact: ArtifactRef | None = None,
    ) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET state_id=?, status='ready', last_outcome=?, claimed_at=NULL, "
                "lease_until=NULL, "
                "external_id=NULL, attention_reason=NULL, attention_detail=NULL, "
                "version=version+1 WHERE id=? AND state_id=? AND status=? AND version=?",
                (target, outcome, token.id.value, token.state_id, token.status, token.version),
            ).rowcount
            if not changed:
                return False
            try:
                self._visit(conn, token.run_id.value, target, max_visits)
            except VisitLimitError as exc:
                conn.execute(
                    "UPDATE tokens SET state_id=?, status='needs_attention', "
                    "attention_reason='visit_limit', attention_detail=? WHERE id=?",
                    (token.state_id, str(exc), token.id.value),
                )
                self._event(conn, token.run_id.value, token.id.value, "needs_attention", str(exc))
                self._queue_notification(
                    conn,
                    token.run_id.value,
                    f"실행 점검 필요: {token.run_id.value} ({exc})",
                    f"attention:{token.id.value}:{token.version + 1}",
                )
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
                if token.fork_id is not None and token.branch_id is not None:
                    conn.execute(
                        "INSERT INTO branch_outputs(fork_id, branch_id, output_name, output) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT(fork_id, branch_id, output_name) "
                        "DO UPDATE SET output=excluded.output",
                        (token.fork_id, token.branch_id, output_name, output),
                    )
                if artifact is not None:
                    conn.execute(
                        "INSERT INTO artifacts(run_id, output_name, digest, path) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT(run_id, output_name) DO UPDATE SET "
                        "digest=excluded.digest, path=excluded.path",
                        (token.run_id.value, output_name, artifact.digest, artifact.path),
                    )
            conn.execute("DELETE FROM pending_task_results WHERE token_id=?", (token.id.value,))
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
            run = conn.execute(
                "SELECT outputs FROM runs WHERE id=?", (token.run_id.value,)
            ).fetchone()
            conn.execute(
                "INSERT INTO forks(id, run_id, join_state, expected, base_outputs) "
                "VALUES (?, ?, ?, ?, ?)",
                (fork_id, token.run_id.value, join, len(branches), str(run["outputs"])),
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

    def wait_for_input(self, token: TokenDTO, prompt: str) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='waiting_input', version=version+1 "
                "WHERE id=? AND state_id=? AND status='ready' AND version=?",
                (token.id.value, token.state_id, token.version),
            ).rowcount
            if changed:
                self._queue_notification(
                    conn,
                    token.run_id.value,
                    f"응답 필요: {prompt}\n토큰: {token.id.value}",
                    f"wait:{token.id.value}:{token.version + 1}",
                )
            return bool(changed)

    def wait_for_host(self, token: TokenDTO) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='waiting_host', version=version+1 WHERE id=? "
                "AND state_id=? AND status='ready' AND version=?",
                (token.id.value, token.state_id, token.version),
            ).rowcount
            if changed:
                self._event(
                    conn, token.run_id.value, token.id.value, "waiting_host", token.state_id
                )
            return bool(changed)

    def host_waiting(self) -> tuple[TokenDTO, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM tokens WHERE status='waiting_host' ORDER BY rowid"
            ).fetchall()
        return tuple(self._token(row) for row in rows)

    def claim_host(self, token: TokenDTO) -> TokenDTO | None:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='executing', claimed_at=?, lease_until=?, "
                "version=version+1 "
                "WHERE id=? AND state_id=? AND status='waiting_host' AND version=?",
                (
                    time.time(),
                    time.time() + self.LEASE_SECONDS,
                    token.id.value,
                    token.state_id,
                    token.version,
                ),
            ).rowcount
            if not changed:
                return None
            number = (
                int(
                    conn.execute(
                        "SELECT COUNT(*) FROM attempts WHERE token_id=?", (token.id.value,)
                    ).fetchone()[0]
                )
                + 1
            )
            conn.execute(
                "INSERT INTO attempts(token_id, number, status, started_at) "
                "VALUES (?, ?, 'executing', ?)",
                (token.id.value, number, time.time()),
            )
            self._event(conn, token.run_id.value, token.id.value, "host_claimed", token.state_id)
            row = conn.execute("SELECT * FROM tokens WHERE id=?", (token.id.value,)).fetchone()
            return self._token(row)

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
            self._queue_notification(
                conn,
                token.run_id.value,
                f"실행 완료: {token.run_id.value} ({result})",
                f"end:{token.run_id.value}",
            )
            return True

    def quota_wait(self, token: TokenDTO, wake_at: float, external_id: str | None) -> bool:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='waiting_quota', wake_at=?, external_id=?, "
                "lease_until=NULL, "
                "version=version+1 "
                "WHERE id=? AND status='executing' AND version=?",
                (wake_at, external_id, token.id.value, token.version),
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

    def mark_attention(self, token: TokenDTO, reason: str = "unknown", detail: str = "") -> None:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE tokens SET status='needs_attention', lease_until=NULL, "
                "attention_reason=?, attention_detail=?, version=version+1 "
                "WHERE id=? AND status NOT IN ('done', 'joined', 'needs_attention') AND version=?",
                (reason, detail[:4000], token.id.value, token.version),
            ).rowcount
            if changed:
                self._event(
                    conn,
                    token.run_id.value,
                    token.id.value,
                    "needs_attention",
                    f"{reason}: {detail[:4000]}",
                )
                self._queue_notification(
                    conn,
                    token.run_id.value,
                    f"실행 점검 필요: {token.run_id.value} (상태: {token.state_id})",
                    f"attention:{token.id.value}:{token.version + 1}",
                )

    def retry_attention(self, run_id: RunId) -> int:
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, state_id FROM tokens WHERE run_id=? AND status='needs_attention' "
                "AND attention_reason IN ('runner_error', 'interrupted', "
                "'artifact_error', 'result_error')",
                (run_id.value,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE tokens SET status='ready', attention_reason=NULL, "
                    "attention_detail=NULL, "
                    "claimed_at=NULL, lease_until=NULL, version=version+1 WHERE id=?",
                    (row["id"],),
                )
                self._event(conn, run_id.value, str(row["id"]), "retry", str(row["state_id"]))
            return len(rows)

    def _recover_expired(
        self, conn: sqlite3.Connection, now: float, run_id: RunId | None = None
    ) -> int:
        query = (
            "SELECT id, run_id, state_id, version FROM tokens WHERE status='executing' "
            "AND (lease_until IS NULL OR lease_until <= ?)"
        )
        parameters: tuple[float] | tuple[float, str] = (now,)
        if run_id is not None:
            query += " AND run_id=?"
            parameters = (now, run_id.value)
        rows = conn.execute(query, parameters).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tokens SET status='needs_attention', lease_until=NULL, "
                "attention_reason='interrupted', attention_detail='execution lease expired', "
                "version=version+1 "
                "WHERE id=?",
                (row["id"],),
            )
            self._event(conn, str(row["run_id"]), str(row["id"]), "recovered_inflight", str(now))
            self._queue_notification(
                conn,
                str(row["run_id"]),
                f"실행 점검 필요: {row['run_id']} (중단된 상태: {row['state_id']})",
                f"attention:{row['id']}:{int(row['version']) + 1}",
            )
        return len(rows)

    def recover_inflight(self, run_id: RunId) -> int:
        with self._transaction() as conn:
            return self._recover_expired(conn, time.time(), run_id)

    def recover_expired_inflight(self, now: float) -> int:
        with self._transaction() as conn:
            return self._recover_expired(conn, now)

    def renew_lease(self, token_id: TokenId) -> bool:
        with self._transaction() as conn:
            return bool(
                conn.execute(
                    "UPDATE tokens SET lease_until=? WHERE id=? AND status='executing'",
                    (time.time() + self.LEASE_SECONDS, token_id.value),
                ).rowcount
            )

    def bind_channel(
        self, run_id: RunId, source: str, actor_id: str, channel_id: str | None
    ) -> None:
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT r.status, d.payload FROM runs r JOIN definitions d "
                "ON r.definition_hash=d.hash WHERE r.id=?",
                (run_id.value,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown run")
            owner = conn.execute(
                "SELECT actor_id FROM run_actors WHERE run_id=? AND source=?",
                (run_id.value, source),
            ).fetchone()
            if owner is not None and owner["actor_id"] != actor_id:
                raise ValueError("run is bound to another actor")
            conn.execute(
                "INSERT OR IGNORE INTO run_actors(run_id, source, actor_id) VALUES (?, ?, ?)",
                (run_id.value, source, actor_id),
            )
            if channel_id is None:
                return
            conn.execute(
                "INSERT OR IGNORE INTO channel_bindings(run_id, source, channel_id) "
                "VALUES (?, ?, ?)",
                (run_id.value, source, channel_id),
            )
            if row["status"] in ("success", "failure"):
                self._queue_notification(
                    conn,
                    run_id.value,
                    f"실행 완료: {run_id.value} ({row['status']})",
                    f"end:{run_id.value}",
                )
            waiting = conn.execute(
                "SELECT id, state_id, version FROM tokens "
                "WHERE run_id=? AND status='waiting_input'",
                (run_id.value,),
            ).fetchall()
            if waiting:
                definition = definition_from_json(str(row["payload"]))
                for token in waiting:
                    prompt = definition.state(str(token["state_id"])).prompt
                    self._queue_notification(
                        conn,
                        run_id.value,
                        f"응답 필요: {prompt}\n토큰: {token['id']}",
                        f"wait:{token['id']}:{token['version']}",
                    )

    def can_access(self, run_id: RunId, source: str, actor_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM run_actors WHERE run_id=? AND source=? AND actor_id=?",
                (run_id.value, source, actor_id),
            ).fetchone()
        return row is not None

    def pending_notifications(self) -> tuple[NotificationDTO, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, source, channel_id, text FROM notifications "
                "WHERE status='pending' ORDER BY rowid LIMIT 100"
            ).fetchall()
        return tuple(
            NotificationDTO(
                id=str(row["id"]),
                source=str(row["source"]),
                channel_id=str(row["channel_id"]),
                text=str(row["text"]),
            )
            for row in rows
        )

    def mark_notification_sent(self, notification_id: str) -> bool:
        with self._transaction() as conn:
            return bool(
                conn.execute(
                    "UPDATE notifications SET status='sent' WHERE id=? AND status='pending'",
                    (notification_id,),
                ).rowcount
            )

    def register_timer(
        self, definition_hash: str, index: int, timer: TimerTrigger, due_at: float
    ) -> None:
        schedule_id = f"{definition_hash}:{index}"
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO schedules(id, definition_hash, cron, timezone, "
                "inputs, next_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    schedule_id,
                    definition_hash,
                    timer.cron,
                    timer.timezone,
                    json.dumps(dict(timer.inputs)),
                    due_at,
                ),
            )

    def due_schedules(self, now: float) -> tuple[ScheduleDTO, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE next_at <= ? ORDER BY next_at", (now,)
            ).fetchall()
        return tuple(
            ScheduleDTO(
                id=str(row["id"]),
                definition_hash=str(row["definition_hash"]),
                cron=str(row["cron"]),
                timezone=str(row["timezone"]),
                inputs=self._pairs(str(row["inputs"])),
                due_at=float(row["next_at"]),
            )
            for row in rows
        )

    def fire_schedule(self, schedule: ScheduleDTO, next_at: float) -> RunId | None:
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE schedules SET next_at=? WHERE id=? AND next_at=?",
                (next_at, schedule.id, schedule.due_at),
            ).rowcount
            if not changed:
                return None
            row = conn.execute(
                "SELECT payload FROM definitions WHERE hash=?", (schedule.definition_hash,)
            ).fetchone()
            if row is None:
                raise ValueError("schedule definition is missing")
            definition = definition_from_json(str(row["payload"]))
            run_id = RunId(uuid.uuid4().hex)
            token_id = TokenId(uuid.uuid4().hex)
            conn.execute(
                "INSERT INTO runs(id, definition_hash, status, inputs, outputs, idempotency_key, "
                "created_at) VALUES (?, ?, 'running', ?, '{}', ?, ?)",
                (
                    run_id.value,
                    schedule.definition_hash,
                    json.dumps(dict(schedule.inputs)),
                    f"timer:{schedule.id}:{schedule.due_at}",
                    time.time(),
                ),
            )
            conn.execute(
                "INSERT INTO tokens(id, run_id, state_id, status) VALUES (?, ?, ?, 'ready')",
                (token_id.value, run_id.value, definition.entry),
            )
            self._visit(
                conn, run_id.value, definition.entry, definition.state(definition.entry).max_visits
            )
            self._event(conn, run_id.value, token_id.value, "timer_fired", schedule.id)
            return run_id
