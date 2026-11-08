"""/walrus slash command and the shared text reports used by the CLI."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Optional

from . import walrus_core as core


def _hermes_home() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except ImportError:
        return None


def status_report(home: Optional[Path] = None, *, live: bool = False, receipts: int = 5) -> str:
    home = home if home is not None else _hermes_home()
    s = core.load_settings(home)
    lines = ["Walrus Memory  ·  long-term memory on Walrus mainnet"]
    if not s.configured:
        lines.append(f"  not configured: set {core.ENV_KEY} and {core.ENV_ACCOUNT} (hermes memory setup)")
        return "\n".join(lines)
    ledger = core.open_ledger(home)
    try:
        c = ledger.counts()
        lines.append(f"  account    {s.account_short}    namespace  {s.namespace}")
        if live:
            try:
                client = core.make_client(s)
                try:
                    health = client.health()
                    ns = client.list_namespaces()
                    total = next((n.memory_count for n in ns.namespaces if n.name == s.namespace), 0)
                    lines.append(f"  relayer    {getattr(health, 'status', '?')} ({s.server_url})")
                    lines.append(f"  memories   {total} in namespace '{s.namespace}' (all machines and agents)")
                finally:
                    client.close()
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  relayer    unreachable: {exc}")
        lines.append(f"  this install  {c['receipts']} stored  ·  {c['pending']} queued  ·  {c['failed']} failed")
        rows = ledger.receipts(receipts)
        if rows:
            lines.append("  latest proofs:")
            for r in rows:
                lines.append(f"    ✓ {core.clip(r['text'], 64):<66} {core.proof_url(r['blob_id'])}")
        pend = ledger.pending(3)
        for p in pend:
            lines.append(f"    … queued {p['kind']}: {core.clip(p['text'].replace(chr(10), ' '), 60)}")
    finally:
        ledger.close()
    return "\n".join(lines)


def proofs_report(home: Optional[Path] = None, limit: int = 10) -> str:
    home = home if home is not None else _hermes_home()
    ledger = core.open_ledger(home)
    try:
        rows = ledger.receipts(limit)
    finally:
        ledger.close()
    if not rows:
        return "No memories stored on Walrus from this install yet."
    lines = [f"Last {len(rows)} memories stored on Walrus (each is a public, verifiable blob):"]
    for r in rows:
        lines.append(f"  ✓ {core.clip(r['text'], 90)}")
        lines.append(f"    {core.proof_url(r['blob_id'])}   ({core.ago(r['created_at'])})")
    return "\n".join(lines)


def recall_report(query: str, home: Optional[Path] = None, limit: int = 6) -> str:
    home = home if home is not None else _hermes_home()
    s = core.load_settings(home)
    if not s.configured:
        return "Walrus Memory is not configured."
    if not query.strip():
        return "Usage: recall <query>"
    client = core.make_client(s)
    try:
        hits = core.recall(client, query, s, limit)
    finally:
        client.close()
    if not hits:
        return f"No memories in '{s.namespace}' match: {query}"
    lines = [f"Recalled from Walrus ({s.namespace}) for: {query}"]
    for h in hits:
        lines.append(f"  {h.relevance:.2f}  {core.clip(h.text, 90)}")
        if h.blob_id:
            lines.append(f"        {core.proof_url(h.blob_id)}")
    return "\n".join(lines)


def verify_report(home: Optional[Path] = None, limit: int = 3, aggregator: str = core.DEFAULT_AGGREGATOR) -> str:
    """Fetch recent blobs straight from a public Walrus aggregator - no relayer, no key."""
    home = home if home is not None else _hermes_home()
    ledger = core.open_ledger(home)
    try:
        rows = ledger.receipts(limit)
    finally:
        ledger.close()
    if not rows:
        return "Nothing to verify yet: no memories stored from this install."
    lines = [f"Verifying {len(rows)} memories directly on Walrus mainnet ({aggregator}):"]
    for r in rows:
        res = core.verify_blob(r["blob_id"], aggregator)
        if res["ok"]:
            enc = "encrypted (plaintext not present)" if core.looks_encrypted(res["body"], r["text"]) \
                else "WARNING: plaintext visible"
            lines.append(f"  ✓ {short(r['blob_id'])}  {res['bytes']} bytes on Walrus  ·  {enc}")
        else:
            lines.append(f"  ✗ {short(r['blob_id'])}  not retrievable (HTTP {res['status']})")
        lines.append(f"    “{core.clip(r['text'], 70)}”")
    return "\n".join(lines)


def short(blob_id: str) -> str:
    return f"{blob_id[:8]}…{blob_id[-4:]}" if len(blob_id) > 14 else blob_id


HELP = """/walrus                 status and latest proofs
/walrus proofs [n]      last n memories with Walruscan proof links
/walrus recall <query>  semantic search across all your machines and agents
/walrus verify [n]      fetch the last n blobs straight from Walrus and check they are encrypted
/walrus retry           re-queue writes that failed"""


def walrus_command(raw_args: str = "", **_: Any) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        parts = (raw_args or "").split()
    sub = parts[0].lower() if parts else "status"
    rest = parts[1:]
    try:
        if sub in ("status", "s"):
            return status_report(live=True)
        if sub in ("proofs", "proof", "p"):
            return proofs_report(limit=_int(rest, 10))
        if sub in ("recall", "search", "r"):
            return recall_report(" ".join(rest))
        if sub in ("verify", "v"):
            return verify_report(limit=_int(rest, 3))
        if sub == "retry":
            ledger = core.open_ledger(_hermes_home())
            try:
                n = ledger.requeue_failed()
            finally:
                ledger.close()
            return f"Re-queued {n} failed write(s)."
        return HELP
    except Exception as exc:  # noqa: BLE001
        return f"walrus: {type(exc).__name__}: {exc}"


def _int(rest: list[str], default: int) -> int:
    try:
        return max(1, min(50, int(rest[0])))
    except (IndexError, ValueError):
        return default
