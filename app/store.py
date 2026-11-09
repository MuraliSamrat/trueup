"""Job store (SQLite). One row per job holding a JSON document, plus an events timeline.

A job lives across several processes (the MCP server Claude Desktop starts, the technician /
owner tools, later the web app), so state goes to a file, not memory.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, MutableMapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "trueup.db"


class JobStatus(str, Enum):
    AWAITING_HOLD = "AWAITING_HOLD"            # order created, customer must approve the hold at PayPal
    BOOKED = "BOOKED"                          # hold placed (authorization), technician on the way
    AWAITING_OWNER = "AWAITING_OWNER"          # big price change, owner must approve first
    AWAITING_CUSTOMER = "AWAITING_CUSTOMER"    # owner approved; customer must approve the extra at PayPal
    SETTLED = "SETTLED"                        # money captured (or hold released)
    CANCELLED = "CANCELLED"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Job(BaseModel):
    job_id: str
    status: JobStatus
    service_id: str
    service_name: str
    slot: str
    problem: str
    customer_ref: str
    hold: dict[str, str]                       # Money as {"amount","currency"}
    mandate: dict[str, Any]                    # Mandate.model_dump(mode="json")
    mandate_sha256: str
    custom_id: str
    order_id: str | None = None
    approve_url: str | None = None
    auth_id: str | None = None
    auth_expires_at: str | None = None
    reported_at: str | None = None
    transcript: str | None = None
    photo_path: str | None = None
    bill_summary: str | None = None
    open_questions: list[str] = Field(default_factory=list)
    dropped_lines: list[str] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)     # LineItem dumps (full bill incl. visit)
    decision: dict[str, Any] | None = None                         # Decision dump
    owner_message: str | None = None
    owner_pending_amount: str | None = None
    supplemental_order_id: str | None = None
    supplemental_approve_url: str | None = None
    settlement: dict[str, Any] | None = None
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)


class JobStore:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, status TEXT, data TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, at TEXT,
                                               kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS claims (job_id TEXT, action TEXT, at REAL, PRIMARY KEY (job_id, action));
        """)
        self.conn.commit()

    # ---- cross-process claims ------------------------------------------------------
    # The web app (PayPal return page, webhooks) and the MCP server (wait_for_paypal_approval) run in
    # different processes and may try to finish the same payment at the same moment. A claim is a
    # row only one process can insert; the other waits for the result instead of charging again.
    def claim(self, job_id: str, action: str, stale_after_s: float = 120.0) -> bool:
        import time as _t
        with self._lock:
            self.conn.execute("DELETE FROM claims WHERE job_id=? AND action=? AND at < ?",
                              (job_id, action, _t.time() - stale_after_s))
            try:
                self.conn.execute("INSERT INTO claims (job_id, action, at) VALUES (?,?,?)", (job_id, action, _t.time()))
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                self.conn.rollback()
                return False

    def release(self, job_id: str, action: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM claims WHERE job_id=? AND action=?", (job_id, action))
            self.conn.commit()

    def save(self, job: Job) -> Job:
        job.updated_at = now_iso()
        with self._lock:
            self.conn.execute(
                "INSERT INTO jobs (job_id, status, data, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET status=excluded.status, data=excluded.data, updated_at=excluded.updated_at",
                (job.job_id, job.status.value, job.model_dump_json(), job.updated_at))
            self.conn.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def require(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"No job {job_id}")
        return job

    def list(self, status: JobStatus | None = None) -> list[Job]:
        q, args = "SELECT data FROM jobs", ()
        if status:
            q, args = q + " WHERE status=?", (status.value,)
        rows = self.conn.execute(q + " ORDER BY updated_at DESC", args).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def find_by_order(self, order_id: str) -> Job | None:
        """The job whose booking hold or supplemental (price change) order has this PayPal id."""
        for j in self.list():
            if order_id in (j.order_id, j.supplemental_order_id):
                return j
        return None

    def booked_slots(self, service_id: str) -> set[str]:
        return {j.slot for j in self.list() if j.service_id == service_id and j.status != JobStatus.CANCELLED}

    def event(self, job_id: str, kind: str, detail: dict[str, Any] | str = "") -> None:
        with self._lock:
            self.conn.execute("INSERT INTO events (job_id, at, kind, detail) VALUES (?,?,?,?)",
                              (job_id, now_iso(), kind, detail if isinstance(detail, str) else json.dumps(detail, default=str)))
            self.conn.commit()

    def events(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT at, kind, detail FROM events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [{"at": a, "kind": k, "detail": d} for a, k, d in rows]


class JobFieldMap(MutableMapping[str, str]):
    """Dict-like view over one string field of every job, so the owner agent's
    'pending' bookkeeping lives in the database instead of process memory."""

    def __init__(self, store: JobStore, field: str) -> None:
        self.store, self.field = store, field

    def __getitem__(self, job_id: str) -> str:
        job = self.store.get(job_id)
        value = getattr(job, self.field, None) if job else None
        if value is None:
            raise KeyError(job_id)
        return value

    def __setitem__(self, job_id: str, value: str) -> None:
        job = self.store.require(job_id)
        setattr(job, self.field, value)
        self.store.save(job)

    def __delitem__(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or getattr(job, self.field) is None:
            raise KeyError(job_id)
        setattr(job, self.field, None)
        self.store.save(job)

    def __iter__(self) -> Iterator[str]:
        return iter([j.job_id for j in self.store.list() if getattr(j, self.field) is not None])

    def __len__(self) -> int:
        return sum(1 for _ in self)
