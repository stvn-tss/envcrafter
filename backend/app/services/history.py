"""Durable history in SQLite: job summaries, reviewable AI plans and an audit log.

The event replay stays in memory (JobEventBus): this keeps what an environment's activity
needs after a restart, and the plans still waiting for their review. Everything here is best
effort: a history that cannot be read or written never fails a job or a request, and a
database file that is not one is set aside at startup and replaced.

The database never holds a secret value or a prompt: job modes, project names, messages
written for users, plan views and the validated stacks of plans (secret *names* only), and
audit lines such as "Notes edited". Its file is owner-only, next to the settings file.

sqlite3 is blocking: every call runs in a worker thread, one at a time (a lock guards the
single connection).
"""

import asyncio
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from app.models.history import ActivityEntry

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    project TEXT,
    service TEXT,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    retryable INTEGER,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_project ON jobs (project, created_at);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    project TEXT,
    action TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_by_project ON audit (project, at);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL,
    body TEXT NOT NULL
);
"""
_INTERRUPTED = "Interrupted: the server stopped."
_PURGE_EVERY = 100  # writes between two purges of what the retention left behind
T = TypeVar("T")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(moment: datetime) -> str:
    """Fixed-width UTC text: it sorts like the moments it stands for."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


class _NewerSchemaError(RuntimeError):
    """The file was written by a newer EnvCrafter: leave it alone, run without history."""


@dataclass(frozen=True)
class JobStart:
    job_id: UUID
    mode: str
    project: str | None
    service: str | None
    message: str  # the job.accepted message
    created_at: datetime


class HistoryStore:
    def __init__(
        self, path: Path, *, retention_days: int, now: Callable[[], datetime] = _utcnow
    ) -> None:
        self._path = path
        self._retention = timedelta(days=retention_days)
        self._now = now
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._writes = 0

    @property
    def available(self) -> bool:
        return self._db is not None

    async def open(self) -> None:
        """Open or create the database. A file that is not a database is set aside and
        replaced; any other failure leaves the history off: the application still starts."""
        try:
            await asyncio.to_thread(self._open)
        except (OSError, sqlite3.Error, _NewerSchemaError):
            logger.exception("History is off: %s cannot be used", self._path)
            self._db = None

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    async def job_started(self, job: JobStart) -> None:
        await self._write(
            "INSERT OR REPLACE INTO jobs (job_id, mode, project, service, status, message, "
            "retryable, created_at, finished_at) VALUES (?, ?, ?, ?, 'running', ?, NULL, ?, NULL)",
            (
                str(job.job_id),
                job.mode,
                job.project,
                job.service,
                job.message,
                _ts(job.created_at),
            ),
        )

    async def job_finished(
        self, job_id: UUID, *, status: str, message: str, retryable: bool | None
    ) -> None:
        await self._write(
            "UPDATE jobs SET status = ?, message = ?, retryable = ?, finished_at = ? "
            "WHERE job_id = ?",
            (status, message, retryable, _ts(self._now()), str(job_id)),
        )

    async def audit(self, action: str, message: str, *, project: str | None = None) -> None:
        """One line of the audit log: who did what, in words written for users."""
        await self._write(
            "INSERT INTO audit (at, project, action, message) VALUES (?, ?, ?, ?)",
            (_ts(self._now()), project, action, message),
        )

    async def activity(
        self, project: str, *, created_at: datetime | None, limit: int = 50
    ) -> list[ActivityEntry] | None:
        """Jobs and audit lines of a project, newest first; None when the history is off.

        `created_at`: when the environment's meta.json was written. The environment began
        with its latest deployment job before that moment: older entries belong to a
        previous environment of the same name, removed since, and are left out."""
        if self._db is None:
            return None
        return await self._call(lambda db: self._activity(db, project, created_at, limit))

    async def save_plan(self, plan_id: UUID, expires_at: datetime, body: str) -> None:
        await self._write(
            "INSERT OR REPLACE INTO plans (plan_id, expires_at, body) VALUES (?, ?, ?)",
            (str(plan_id), _ts(expires_at), body),
        )

    async def load_plans(self) -> list[str]:
        """The stored plans still waiting for their review, oldest first."""
        rows = await self._call(
            lambda db: db.execute(
                "SELECT body FROM plans WHERE expires_at > ? ORDER BY expires_at",
                (_ts(self._now()),),
            ).fetchall()
        )
        return [row[0] for row in rows or []]

    # --- Worker-thread side ---------------------------------------------------------

    async def _write(self, sql: str, parameters: tuple[object, ...]) -> None:
        await self._call(lambda db: self._execute(db, sql, parameters))

    async def _call(self, work: Callable[[sqlite3.Connection], T]) -> T | None:
        if self._db is None:
            return None
        try:
            return await asyncio.to_thread(self._locked, work)
        except Exception:
            logger.warning("History unavailable for this operation", exc_info=True)
            return None

    def _locked(self, work: Callable[[sqlite3.Connection], T]) -> T | None:
        with self._lock:
            if self._db is None:
                return None
            return work(self._db)

    def _execute(self, db: sqlite3.Connection, sql: str, parameters: tuple[object, ...]) -> None:
        db.execute(sql, parameters)
        self._writes += 1
        if self._writes % _PURGE_EVERY == 0:
            self._purge(db, self._now())

    def _open(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._create_file()
        try:
            self._connect()
        except sqlite3.DatabaseError:
            aside = self._path.with_name(f"{self._path.name}.corrupt-{self._now():%Y%m%dT%H%M%S}")
            logger.error("Unreadable history database: moved to %s, starting a new one", aside)
            os.replace(self._path, aside)
            self._create_file()
            self._connect()

    def _create_file(self) -> None:
        # Owner-only from the start: sqlite would create it with the process umask.
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self._path, 0o600)

    def _connect(self) -> None:
        db = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        try:
            # The first read of the file: a file that is not a database fails here.
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise _NewerSchemaError(f"history schema {version} is newer than this version")
            # A write-ahead log synced at checkpoints: every job writes twice, and a history
            # may lose its last lines on a power cut, never its consistency.
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.executescript(_SCHEMA)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            now = self._now()
            # A job still running belongs to a process that stopped before it ended.
            db.execute(
                "UPDATE jobs SET status = 'failed', message = ?, finished_at = ? "
                "WHERE status IN ('queued', 'running')",
                (_INTERRUPTED, _ts(now)),
            )
            self._purge(db, now)
        except BaseException:
            db.close()
            raise
        self._db = db

    def _purge(self, db: sqlite3.Connection, now: datetime) -> None:
        horizon = _ts(now - self._retention)
        db.execute("DELETE FROM jobs WHERE created_at < ?", (horizon,))
        db.execute("DELETE FROM audit WHERE at < ?", (horizon,))
        db.execute("DELETE FROM plans WHERE expires_at <= ?", (_ts(now),))

    def _close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    @staticmethod
    def _activity(
        db: sqlite3.Connection, project: str, created_at: datetime | None, limit: int
    ) -> list[ActivityEntry]:
        since = ""
        if created_at is not None:
            row = db.execute(
                "SELECT MAX(created_at) FROM jobs WHERE project = ? "
                "AND mode IN ('template', 'prompt', 'plan') AND created_at <= ?",
                (project, _ts(created_at)),
            ).fetchone()
            since = row[0] if row is not None and row[0] else _ts(created_at)
        jobs = db.execute(
            "SELECT job_id, mode, service, status, message, created_at, finished_at FROM jobs "
            "WHERE project = ? AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (project, since, limit),
        ).fetchall()
        audits = db.execute(
            "SELECT at, action, message FROM audit WHERE project = ? AND at >= ? "
            "ORDER BY at DESC LIMIT ?",
            (project, since, limit),
        ).fetchall()
        entries = [
            ActivityEntry(
                at=datetime.fromisoformat(finished or created),
                kind="job",
                action=mode,
                status=status,
                message=message,
                service=service,
                job_id=UUID(job_id),
            )
            for job_id, mode, service, status, message, created, finished in jobs
        ]
        entries += [
            ActivityEntry(
                at=datetime.fromisoformat(at),
                kind="audit",
                action=action,
                status=None,
                message=message,
            )
            for at, action, message in audits
        ]
        return sorted(entries, key=lambda entry: entry.at, reverse=True)[:limit]
