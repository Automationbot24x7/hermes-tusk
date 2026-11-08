"""Backend for the Tusk dashboard tab, mounted by Hermes at /api/plugins/tusk/.

Routes sit behind the dashboard's auth gate. Blocking work (SQLite, the SDK, the Walrus
aggregator) runs in worker threads so the dashboard's event loop never stalls.
core.py and outbox.py are loaded by file path: the plugin package itself (and with it
the agent runtime) is never imported into the dashboard process.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_BLOB_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_NAMESPACE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_tusk_dashboard_{name}", _PLUGIN_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = _load("core")
outbox = _load("outbox")
router = APIRouter()

_clients: dict[str, Any] = {}
_clients_lock = threading.Lock()


def _client(s) -> Any:
    """One SDK client per credential set, reused across requests (the SDK client is thread-safe)."""
    key = hashlib.sha256(f"{s.key}|{s.account_id}|{s.server_url}|{s.namespace}".encode()).hexdigest()
    with _clients_lock:
        if key not in _clients:
            _clients[key] = core.make_client(s)
        return _clients[key]


def _home() -> Path | None:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return None
    return Path(get_hermes_home())


def _with_ledger(fn):
    ledger = outbox.open_ledger(core.data_dir(_home()))
    try:
        return fn(ledger)
    finally:
        ledger.close()


def _overview() -> dict[str, Any]:
    s = core.load_settings(_home())

    def read(ledger) -> dict[str, Any]:
        now = time.time()
        pending = [
            {
                "kind": p["kind"],
                "text": core.clip(p["text"].replace("\n", " "), 140),
                "attempts": p["attempts"],
                "state": "sending" if p["claimed_until"] > now else ("retrying" if p["next_at"] > now else "queued"),
                "retry_in_s": max(0, int(p["next_at"] - now)),
                "error": p["last_error"] or "",
            }
            for p in ledger.pending(20)
        ]
        failed = [
            {"kind": f["kind"], "text": core.clip(f["text"], 140), "error": f["last_error"] or ""}
            for f in ledger.failed(20)
        ]
        receipts = [
            {
                "text": r["text"],
                "namespace": r["namespace"],
                "blob_id": r["blob_id"],
                "proof_url": core.proof_url(r["blob_id"]),
                "stored_at": r["created_at"],
            }
            for r in ledger.receipts(30)
        ]
        return {"counts": ledger.counts(), "pending": pending, "failed": failed, "receipts": receipts}

    return {
        "configured": s.configured,
        "account": s.account_short,
        "namespace": s.namespace,
        "server_url": s.server_url,
        "aggregator": core.DEFAULT_AGGREGATOR,
        "signup_url": core.SIGNUP_URL,
        **_with_ledger(read),
    }


def _namespaces() -> dict[str, Any]:
    s = core.load_settings(_home())
    if not s.configured:
        return {"namespaces": [], "current": s.namespace}
    spaces = _client(s).list_namespaces().namespaces
    return {
        "current": s.namespace,
        "namespaces": sorted(
            ({"name": n.name, "memories": n.memory_count} for n in spaces), key=lambda n: -n["memories"]
        ),
    }


def _recall(q: str, namespace: str | None, limit: int) -> dict[str, Any]:
    s = core.load_settings(_home())
    if not s.configured:
        raise HTTPException(409, "Tusk is not configured")
    hits = core.recall(_client(s), q, s, limit, namespace)
    return {"namespace": namespace or s.namespace, "results": [h.as_dict() for h in hits]}


def _verify(blob_id: str, text: str | None) -> dict[str, Any]:
    receipt = _with_ledger(lambda ledger: ledger.find_receipt(blob_id))
    remembered = receipt["text"] if receipt else text
    started = time.time()
    res = core.verify_blob(blob_id)
    out: dict[str, Any] = {
        "blob_id": blob_id,
        "ok": res["ok"],
        "status": res["status"],
        "bytes": res["bytes"],
        "took_ms": int((time.time() - started) * 1000),
        "aggregator": core.DEFAULT_AGGREGATOR,
        "proof_url": core.proof_url(blob_id),
        "hex": core.hex_preview(res["body"], 48),
    }
    if res["ok"] and remembered:
        out["plaintext_absent"] = core.plaintext_absent(res["body"], remembered)
    return out


@router.get("/overview")
async def overview() -> dict[str, Any]:
    return await asyncio.to_thread(_overview)


@router.get("/namespaces")
async def namespaces() -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_namespaces)
    except Exception as exc:
        raise HTTPException(502, f"relayer unavailable: {exc}") from exc


@router.get("/recall")
async def recall(
    q: str = Query(..., min_length=1, max_length=500),
    namespace: str | None = Query(None),
    limit: int = Query(8, ge=1, le=20),
) -> dict[str, Any]:
    if namespace and not _NAMESPACE.match(namespace):
        raise HTTPException(400, "invalid namespace")
    try:
        return await asyncio.to_thread(_recall, q, namespace, limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"recall failed: {exc}") from exc


@router.get("/verify")
async def verify(blob_id: str = Query(...), text: str | None = Query(None, max_length=1000)) -> dict[str, Any]:
    if not _BLOB_ID.match(blob_id):
        raise HTTPException(400, "invalid blob id")
    return await asyncio.to_thread(_verify, blob_id, text)


@router.post("/retry")
async def retry() -> dict[str, Any]:
    return {"requeued": await asyncio.to_thread(_with_ledger, lambda ledger: ledger.requeue_failed())}
