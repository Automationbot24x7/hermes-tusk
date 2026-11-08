"""Durable outbox and receipts for Walrus writes.

Every write is first committed to a local SQLite file, then a background ``Writer``
sends it, paced under the relayer's per-key rate limit and retried with backoff.
Pending writes therefore survive crashes, closed terminals and restarts. Each stored
memory leaves a receipt with its Walrus blob id.

Standard library only and no package-relative imports, so the dashboard backend can
load this file by path.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger("tusk")

WRITE_GAP_S = 11.0  # the managed relayer allows ~6 writes per minute per delegate key
LEASE_S = 300.0  # longer than the slowest job (JOB_TIMEOUT_MS) so leases never expire mid-send
ORPHAN_LEASE_S = 30.0  # what is left of a lease whose writer is shutting down mid-send
JOB_TIMEOUT_MS = 150_000
MAX_ATTEMPTS = 6
DEDUPE_DAYS = 30  # identical content is not re-sent within this window
DB_NAME = "outbox.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- 'fact' (stored verbatim) or 'turn' (sent for fact extraction)
    namespace TEXT NOT NULL,
    text TEXT NOT NULL,              -- cleared once sent: raw dialogue does not linger on disk
    idem TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    claimed_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    done_at REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_at);
CREATE INDEX IF NOT EXISTS outbox_idem ON outbox(idem);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    text TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


def content_key(kind: str, namespace: str, text: str) -> str:
    """Stable id for a write; also sent as the relayer idempotency key for facts."""
    return hashlib.sha256(f"{kind}\x1f{namespace}\x1f{text}".encode()).hexdigest()[:48]


def clip(text: str, limit: int) -> str:
    """Same as core.clip; repeated so this module stays loadable on its own."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Ledger:
    """Outbox plus receipts in one SQLite file. Safe across threads and processes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            # Created private before SQLite opens it; SQLite gives its -wal/-shm files the same mode.
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with contextlib.suppress(sqlite3.DatabaseError):  # e.g. network filesystems keep the default journal
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.execute("DELETE FROM outbox WHERE status='done' AND done_at < ?", (time.time() - DEDUPE_DAYS * 86400,))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextlib.contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE takes SQLite's write lock up front, so check-then-write is atomic
        across processes (a running Hermes and `hermes tusk flush`, say)."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # -- writes ---------------------------------------------------------------

    def enqueue(self, kind: str, namespace: str, text: str) -> bool:
        """Queue a write unless the same content is queued, parked, or was sent recently."""
        text = (text or "").strip()
        if not text:
            return False
        key = content_key(kind, namespace, text)
        with self._write_txn() as db:
            dup = db.execute(
                "SELECT 1 FROM outbox WHERE idem=? AND (status!='done' OR done_at >= ?) LIMIT 1",
                (key, time.time() - DEDUPE_DAYS * 86400),
            ).fetchone()
            if dup:
                return False
            db.execute(
                "INSERT INTO outbox(kind, namespace, text, idem, created_at) VALUES (?,?,?,?,?)",
                (kind, namespace, text, key, time.time()),
            )
        return True

    def claim_next(self, lease_s: float = LEASE_S) -> sqlite3.Row | None:
        """Lease the oldest due job so no other writer sends it at the same time."""
        now = time.time()
        with self._write_txn() as db:
            row = db.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_at<=? AND claimed_until<=? ORDER BY id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is not None:
                db.execute("UPDATE outbox SET claimed_until=? WHERE id=?", (now + lease_s, row["id"]))
        return row

    def complete(self, row: sqlite3.Row, receipts: Iterable[tuple[str, str]]) -> int:
        """Mark a job sent, record one receipt per stored memory, and drop the queued text."""
        stored = 0
        with self._write_txn() as db:
            for text, blob_id in receipts:
                if blob_id:
                    db.execute(
                        "INSERT INTO receipts(kind, namespace, text, blob_id, created_at) VALUES (?,?,?,?,?)",
                        (row["kind"], row["namespace"], text, blob_id, time.time()),
                    )
                    stored += 1
            db.execute(
                "UPDATE outbox SET status='done', text='', done_at=?, claimed_until=0, last_error=NULL WHERE id=?",
                (time.time(), row["id"]),
            )
        return stored

    def retry(self, row: sqlite3.Row, error: str, delay_s: float) -> str:
        attempts = int(row["attempts"]) + 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        with self._write_txn() as db:
            db.execute(
                "UPDATE outbox SET attempts=?, status=?, next_at=?, claimed_until=0, last_error=? WHERE id=?",
                (attempts, status, time.time() + delay_s, clip(error, 300), row["id"]),
            )
        return status

    def shorten_lease(self, row_id: int, seconds: float = ORPHAN_LEASE_S) -> None:
        """Let another writer pick a job up soon when its current writer is going away mid-send."""
        with self._write_txn() as db:
            db.execute(
                "UPDATE outbox SET claimed_until=MIN(claimed_until, ?) WHERE id=? AND status='pending'",
                (time.time() + seconds, row_id),
            )

    def requeue_failed(self) -> int:
        with self._write_txn() as db:
            return db.execute(
                "UPDATE outbox SET status='pending', attempts=0, next_at=0, claimed_until=0 WHERE status='failed'"
            ).rowcount

    # -- reads ----------------------------------------------------------------

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def counts(self) -> dict[str, int]:
        with self._lock:
            out = {
                r["status"]: r["n"]
                for r in self._db.execute("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")
            }
            out["receipts"] = self._db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            out["in_flight"] = self._db.execute(
                "SELECT COUNT(*) FROM outbox WHERE status='pending' AND claimed_until>?", (time.time(),)
            ).fetchone()[0]
        for k in ("pending", "done", "failed"):
            out.setdefault(k, 0)
        return out

    def pending(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT id, kind, text, attempts, next_at, claimed_until, last_error FROM outbox "
            "WHERE status='pending' ORDER BY id LIMIT ?",
            (limit,),
        )

    def failed(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT id, kind, text, attempts, last_error FROM outbox WHERE status='failed' ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def receipts(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT kind, namespace, text, blob_id, created_at FROM receipts ORDER BY id DESC LIMIT ?", (limit,)
        )

    def find_receipt(self, blob_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT kind, namespace, text, blob_id, created_at FROM receipts WHERE blob_id=? ORDER BY id DESC LIMIT 1",
            (blob_id,),
        )
        return rows[0] if rows else None


def open_ledger(data_dir: Path) -> Ledger:
    return Ledger(Path(data_dir) / DB_NAME)


# --------------------------------------------------------------------------- sending


def is_rate_limited(exc: BaseException) -> bool:
    if getattr(exc, "status", None) == 429 or type(exc).__name__ == "MemWalRateLimited":
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate-limit" in text


def retry_after(exc: BaseException, default: float) -> float:
    try:
        seconds = float(getattr(exc, "retry_after", None))
    except (TypeError, ValueError):
        return default
    return min(seconds + 1.0, 600.0) if seconds > 0 else default


def send_job(client: Any, row: Any) -> list[tuple[str, str]]:
    """Run one outbox job against Walrus Memory; return (text, blob_id) per stored memory."""
    kind, namespace, text = row["kind"], row["namespace"], row["text"]
    if kind == "turn":
        from memwal import RememberBulkOptions

        # The relayer extracts facts inside its TEE and stores each one as its own memory.
        # Settled results come back in the same order as the extracted facts.
        res = client.analyze_and_wait(text, namespace=namespace, opts=RememberBulkOptions(timeout_ms=JOB_TIMEOUT_MS))
        stored = [
            (f.text, r.blob_id)
            for f, r in zip(res.facts or [], res.results or [], strict=False)
            if r.status == "done" and r.blob_id
        ]
        if res.facts and not stored and not res.timed_out:
            raise RuntimeError(f"fact extraction stored nothing ({res.failed} of {res.total} failed)")
        return stored
    res = client.remember_and_wait(text, namespace=namespace, timeout_ms=JOB_TIMEOUT_MS, idempotency_key=row["idem"])
    return [(text, res.blob_id)]


class Writer:
    """Drains the outbox one job at a time, paced, in a background thread."""

    def __init__(
        self,
        ledger: Ledger,
        client_factory: Callable[[], Any],
        *,
        gap_s: float = WRITE_GAP_S,
        spawn: Callable[..., threading.Thread] | None = None,
    ):
        self.ledger = ledger
        self._client_factory = client_factory
        self._client: Any = None
        self.gap_s = gap_s
        self._spawn = spawn
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sending: int | None = None
        self._last_send = 0.0

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        spawn = self._spawn or (lambda target, name: threading.Thread(target=target, name=name, daemon=True))
        self._thread = spawn(self._run, name="tusk-writer")
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> bool:
        """Ask the thread to stop; True if it has. If a job is still mid-send (one-shot runs exit
        long before a 20-30 s write finishes), its lease is cut to ORPHAN_LEASE_S so the next writer
        resumes it soon; facts carry an idempotency key, so a resend cannot store them twice."""
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        if self.alive and self._sending is not None:
            with contextlib.suppress(sqlite3.Error):
                self.ledger.shorten_lease(self._sending)
        return not self.alive

    def step(self) -> dict[str, Any] | None:
        """Send at most one due job; None when nothing was due."""
        wait = self._last_send + self.gap_s - time.time()
        if wait > 0 and self.ledger.counts()["pending"] and self._stop.wait(wait):
            return None  # paced before claiming, so a shutdown never strands a lease
        row = self.ledger.claim_next()
        if row is None:
            return None
        self._last_send = time.time()
        self._sending = row["id"]
        try:
            receipts = send_job(self.client, row)
        except Exception as exc:  # every failure is retried later or parked as failed, never dropped
            self._sending = None
            if is_rate_limited(exc):
                delay = retry_after(exc, 30.0)
            else:
                delay = min(300.0, 10.0 * 2 ** int(row["attempts"]))
            status = self.ledger.retry(row, f"{type(exc).__name__}: {exc}", delay)
            logger.warning("tusk: %s write %s: %s", row["kind"], status, exc)
            return {"ok": False, "kind": row["kind"], "error": str(exc), "status": status}
        self._sending = None
        stored = self.ledger.complete(row, receipts)
        return {"ok": True, "kind": row["kind"], "stored": stored, "receipts": receipts}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.step()
            except sqlite3.ProgrammingError:
                return  # ledger closed under us during interpreter shutdown
            except Exception as exc:
                logger.warning("tusk: writer error: %s", exc)
                result = None
            if result is None:
                self._wake.wait(2.0)  # also polls, so rows re-queued by another process are picked up
                self._wake.clear()
