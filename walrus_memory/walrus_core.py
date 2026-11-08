"""Walrus Memory plumbing shared by the provider, the /walrus command and the CLI.

* ``Settings``  - resolves credentials (through Hermes' secret scope) and options.
* ``Ledger``    - a small SQLite file in the plugin data dir: a durable outbox of
                  pending writes plus a receipt for every memory stored on Walrus.
* ``Writer``    - drains the outbox in a background thread, paced under the
                  relayer's rate limit, retrying with backoff. Pending writes
                  survive crashes and restarts because they live in SQLite.
* helpers       - recall formatting, Walruscan proof links and direct blob
                  verification against a public Walrus aggregator.

Nothing here reads credential files: secrets come from ``agent.secret_scope``
(or the process environment when Hermes is not present, e.g. in tests).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("walrus-memory")

PLUGIN_NAME = "walrus-memory"
DEFAULT_SERVER_URL = "https://relayer.memory.walrus.xyz"
DEFAULT_NAMESPACE = "default"
DEFAULT_AGGREGATOR = "https://aggregator.walrus-mainnet.walrus.space"
WALRUSCAN_BLOB = "https://walruscan.com/mainnet/blob/{blob_id}"

ENV_KEY = "MEMWAL_PRIVATE_KEY"
ENV_ACCOUNT = "MEMWAL_ACCOUNT_ID"
ENV_SERVER = "MEMWAL_SERVER_URL"
ENV_NAMESPACE = "MEMWAL_NAMESPACE"

# The managed relayer allows about 6 remembers per minute per delegate key.
WRITE_GAP_S = 11.0
MAX_ATTEMPTS = 6
MAX_TURN_CHARS = 2400
MAX_FACT_CHARS = 1200


# --------------------------------------------------------------------------- settings


def _secret(name: str) -> str:
    """Read a credential through Hermes' profile-aware secret scope."""
    try:
        from agent.secret_scope import get_secret  # type: ignore

        value = get_secret(name)
    except ImportError:  # outside Hermes (unit tests, standalone use)
        value = os.environ.get(name)
    return (value or "").strip()


@dataclass
class Settings:
    key: str = ""
    account_id: str = ""
    server_url: str = DEFAULT_SERVER_URL
    namespace: str = DEFAULT_NAMESPACE
    capture_turns: bool = True
    recall_limit: int = 6
    max_distance: float = 0.8
    prefetch_timeout_s: float = 6.0

    @property
    def configured(self) -> bool:
        return bool(self.key and self.account_id)

    @property
    def account_short(self) -> str:
        a = self.account_id
        return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def config_path(hermes_home: Path) -> Path:
    return Path(hermes_home) / f"{PLUGIN_NAME}.json"


def load_settings(hermes_home: Optional[Path]) -> Settings:
    """Secrets from the secret scope; non-secret options from $HERMES_HOME/walrus-memory.json."""
    opts: dict[str, Any] = {}
    if hermes_home is not None:
        try:
            opts = json.loads(config_path(hermes_home).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            opts = {}
    s = Settings(
        key=_secret(ENV_KEY),
        account_id=_secret(ENV_ACCOUNT),
        server_url=_secret(ENV_SERVER) or str(opts.get("server_url") or DEFAULT_SERVER_URL),
        namespace=_secret(ENV_NAMESPACE) or str(opts.get("namespace") or DEFAULT_NAMESPACE),
    )
    if "capture_turns" in opts:
        s.capture_turns = bool(opts["capture_turns"])
    for field, cast in (("recall_limit", int), ("max_distance", float), ("prefetch_timeout_s", float)):
        if field in opts:
            try:
                setattr(s, field, cast(opts[field]))
            except (TypeError, ValueError):
                pass
    return s


def data_dir(hermes_home: Optional[Path] = None) -> Path:
    """``<hermes home>/plugin-data/walrus-memory`` - survives plugin updates and removal."""
    if hermes_home is None:
        try:
            from plugins.plugin_storage import plugin_data_dir  # type: ignore

            return plugin_data_dir(PLUGIN_NAME)
        except ImportError:
            hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    path = Path(hermes_home) / "plugin-data" / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_client(settings: Settings):
    """Create the official Walrus Memory SDK client (sync flavour)."""
    from memwal import MemWalSync  # imported lazily: is_available() must stay cheap

    return MemWalSync.create(
        key=settings.key,
        account_id=settings.account_id,
        server_url=settings.server_url,
        namespace=settings.namespace,
    )


def proof_url(blob_id: str) -> str:
    return WALRUSCAN_BLOB.format(blob_id=blob_id)


def short_id(blob_id: str) -> str:
    return blob_id[:10] if blob_id else "pending"


def idempotency_key(kind: str, namespace: str, text: str) -> str:
    return hashlib.sha256(f"{kind}\x1f{namespace}\x1f{text}".encode("utf-8")).hexdigest()[:48]


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- ledger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    text TEXT NOT NULL,
    idem TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_at REAL NOT NULL DEFAULT 0,
    claimed_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    namespace TEXT NOT NULL,
    text TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, next_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Ledger:
    """Durable outbox + receipts. Safe to share between threads and processes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writes ---------------------------------------------------------------

    def enqueue(self, kind: str, namespace: str, text: str) -> bool:
        """Queue a write. Identical content is queued once (idempotent). Returns True if new."""
        text = (text or "").strip()
        if not text:
            return False
        idem = idempotency_key(kind, namespace, text)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO outbox(kind, namespace, text, idem, created_at) VALUES (?,?,?,?,?)",
                (kind, namespace, text, idem, time.time()),
            )
            return cur.rowcount == 1

    def claim_next(self, lease_s: float = 180.0) -> Optional[sqlite3.Row]:
        """Atomically lease the oldest due row so two writers never send the same job."""
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_at<=? AND claimed_until<=? "
                "ORDER BY id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            cur = self._db.execute(
                "UPDATE outbox SET claimed_until=? WHERE id=? AND claimed_until<=?",
                (now + lease_s, row["id"], now),
            )
            return row if cur.rowcount == 1 else None

    def complete(self, row: sqlite3.Row, receipts: Iterable[tuple[str, str]]) -> int:
        """Mark a job done and record one receipt per stored memory (text, blob_id)."""
        n = 0
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for text, blob_id in receipts:
                    if not blob_id:
                        continue
                    self._db.execute(
                        "INSERT INTO receipts(kind, namespace, text, blob_id, created_at) VALUES (?,?,?,?,?)",
                        (row["kind"], row["namespace"], text, blob_id, time.time()),
                    )
                    n += 1
                self._db.execute("UPDATE outbox SET status='done', claimed_until=0, last_error=NULL WHERE id=?",
                                 (row["id"],))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return n

    def release(self, row: sqlite3.Row) -> None:
        """Give a leased row back untouched (e.g. on shutdown before it was sent)."""
        with self._lock:
            self._db.execute("UPDATE outbox SET claimed_until=0 WHERE id=?", (row["id"],))

    def retry(self, row: sqlite3.Row, error: str, delay_s: float) -> str:
        attempts = int(row["attempts"]) + 1
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        with self._lock:
            self._db.execute(
                "UPDATE outbox SET attempts=?, status=?, next_at=?, claimed_until=0, last_error=? WHERE id=?",
                (attempts, status, time.time() + delay_s, clip(error, 300), row["id"]),
            )
        return status

    def requeue_failed(self) -> int:
        with self._lock:
            cur = self._db.execute(
                "UPDATE outbox SET status='pending', attempts=0, next_at=0, claimed_until=0 WHERE status='failed'"
            )
            return cur.rowcount

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, json.dumps(value)))

    # -- reads ----------------------------------------------------------------

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def counts(self) -> dict[str, int]:
        with self._lock:
            out = {r["status"]: r["n"] for r in self._db.execute(
                "SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")}
            out["receipts"] = self._db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        for k in ("pending", "done", "failed"):
            out.setdefault(k, 0)
        return out

    def pending(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, text, attempts, next_at, last_error FROM outbox "
                "WHERE status='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def failed(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, kind, text, attempts, last_error FROM outbox WHERE status='failed' "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def receipts(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, namespace, text, blob_id, created_at FROM receipts ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]


def open_ledger(hermes_home: Optional[Path] = None) -> Ledger:
    return Ledger(data_dir(hermes_home) / "ledger.db")


# --------------------------------------------------------------------------- writer


def _is_rate_limited(exc: BaseException) -> bool:
    if getattr(exc, "status", None) == 429:
        return True
    name = type(exc).__name__
    text = str(exc).lower()
    return name == "MemWalRateLimited" or "429" in text or "rate limit" in text or "rate-limit" in text


def _retry_after(exc: BaseException, default: float) -> float:
    for attr in ("retry_after", "retry_after_s", "retry_after_seconds"):
        value = getattr(exc, attr, None)
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return min(seconds + 1.0, 600.0)
    return default


def send_job(client: Any, row: Any) -> list[tuple[str, str]]:
    """Execute one outbox job against Walrus Memory and return (text, blob_id) receipts."""
    kind, namespace, text = row["kind"], row["namespace"], row["text"]
    if kind == "turn":
        # Server-side fact extraction (inside the relayer's TEE); each fact becomes a memory.
        result = client.analyze_and_wait(text, namespace=namespace)
        return [(f.text, f.blob_id) for f in (result.facts or []) if getattr(f, "blob_id", None)]
    result = client.remember_and_wait(text, namespace=namespace, timeout_ms=120_000,
                                      idempotency_key=row["idem"])
    return [(text, result.blob_id)]


class Writer:
    """Background drain of the outbox. One per provider instance; restart-safe."""

    def __init__(self, ledger: Ledger, client_factory: Callable[[], Any], *,
                 gap_s: float = WRITE_GAP_S, spawn: Optional[Callable[..., threading.Thread]] = None,
                 on_stored: Optional[Callable[[list[tuple[str, str]]], None]] = None):
        self.ledger = ledger
        self._client_factory = client_factory
        self._client = None
        self.gap_s = gap_s
        self._spawn = spawn
        self._on_stored = on_stored
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_send = 0.0

    @property
    def client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        if self._spawn is not None:
            self._thread = self._spawn(self._run, name="walrus-memory-writer")
        else:
            self._thread = threading.Thread(target=self._run, name="walrus-memory-writer", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)

    def step(self) -> Optional[dict[str, Any]]:
        """Send at most one due job. Returns a summary dict, or None when nothing was due."""
        wait = self._last_send + self.gap_s - time.time()
        if wait > 0 and self.ledger.counts()["pending"]:
            if self._stop.wait(wait):  # pace before claiming, so shutdown never strands a lease
                return None
        row = self.ledger.claim_next()
        if row is None:
            return None
        self._last_send = time.time()
        try:
            receipts = send_job(self.client, row)
        except Exception as exc:  # noqa: BLE001 - every failure is retried or parked
            if _is_rate_limited(exc):
                delay = _retry_after(exc, 30.0)
            else:
                delay = min(300.0, 10.0 * (2 ** int(row["attempts"])))
            status = self.ledger.retry(row, f"{type(exc).__name__}: {exc}", delay)
            logger.warning("walrus-memory write %s (%s): %s", status, row["kind"], exc)
            return {"ok": False, "kind": row["kind"], "error": str(exc), "status": status}
        stored = self.ledger.complete(row, receipts)
        if receipts and self._on_stored:
            try:
                self._on_stored(receipts)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "kind": row["kind"], "stored": stored, "receipts": receipts}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.step()
            except Exception as exc:  # noqa: BLE001 - never let the writer die
                logger.warning("walrus-memory writer error: %s", exc)
                result = None
            if result is None:
                self._wake.wait(2.0)
                self._wake.clear()


# --------------------------------------------------------------------------- recall + verify


@dataclass
class Hit:
    text: str
    blob_id: str
    distance: float
    created_at: str = ""

    @property
    def relevance(self) -> float:
        return round(max(0.0, 1.0 - float(self.distance)), 3)

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "relevance": self.relevance, "blob_id": self.blob_id,
                "proof_url": proof_url(self.blob_id) if self.blob_id else "", "created_at": self.created_at}


def recall(client: Any, query: str, settings: Settings, limit: Optional[int] = None) -> list[Hit]:
    from memwal import RecallParams

    params = RecallParams(query=query, limit=limit or settings.recall_limit, namespace=settings.namespace,
                          max_distance=settings.max_distance)
    result = client.recall(params)
    hits = []
    for m in result.results or []:
        hits.append(Hit(text=str(m.text), blob_id=str(m.blob_id or ""), distance=float(m.distance or 0.0),
                        created_at=str(getattr(m, "created_at", "") or "")))
    return hits


def format_recall(hits: list[Hit], namespace: str) -> str:
    if not hits:
        return ""
    lines = [
        f'<walrus-memory namespace="{namespace}">',
        "Long-term memories recalled from Walrus (decentralized, encrypted, owned by the user). "
        "Use them as background facts. If the user asks where a fact came from, cite its proof id; "
        "walrus_memory_search returns the full Walruscan proof link.",
    ]
    for h in hits:
        lines.append(f"- {clip(h.text, 400)}  [proof:{short_id(h.blob_id)}]")
    lines.append("</walrus-memory>")
    return "\n".join(lines)


def verify_blob(blob_id: str, aggregator: str = DEFAULT_AGGREGATOR, timeout: float = 20.0) -> dict[str, Any]:
    """Fetch a blob straight from a public Walrus aggregator (no relayer, no key).

    Proves the memory exists on Walrus mainnet, and that what is stored there is
    ciphertext: the stored bytes must not contain the plaintext we remembered.
    """
    url = f"{aggregator.rstrip('/')}/v1/blobs/{blob_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "hermes-walrus-memory"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
            body = resp.read(1_000_000)
            return {"blob_id": blob_id, "ok": True, "status": resp.status, "bytes": len(body), "body": body}
    except urllib.error.HTTPError as exc:
        return {"blob_id": blob_id, "ok": False, "status": exc.code, "bytes": 0, "body": b""}
    except Exception as exc:  # noqa: BLE001
        return {"blob_id": blob_id, "ok": False, "status": 0, "bytes": 0, "body": b"", "error": str(exc)}


def looks_encrypted(body: bytes, plaintext: str) -> bool:
    """True when none of the plaintext's longer words appear in the stored bytes."""
    words = [w for w in plaintext.split() if len(w) >= 5][:8]
    if not words or not body:
        return bool(body)
    lowered = body.lower()
    return not any(w.lower().encode("utf-8", "ignore") in lowered for w in words)


def ago(ts: float) -> str:
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"
