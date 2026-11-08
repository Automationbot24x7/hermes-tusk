"""The /tusk slash command, plus the text reports it shares with `hermes tusk`."""

from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import Any

from . import core, outbox


def active_home() -> Path | None:
    """HERMES_HOME of the profile running this command (Hermes resolves it per context)."""
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return None
    return Path(get_hermes_home())


def _ledger(home: Path | None) -> outbox.Ledger:
    return outbox.open_ledger(core.data_dir(home))


def status_report(home: Path | None = None, *, live: bool = True, proofs: int = 5) -> str:
    s = core.load_settings(home)
    lines = ["Tusk · Hermes memory on Walrus mainnet"]
    if not s.configured:
        lines.append(f"  Not configured. Get a delegate key at {core.SIGNUP_URL}, then run: hermes memory setup")
        return "\n".join(lines)
    lines.append(f"  account     {s.account_short}      namespace  {s.namespace}")
    if live:
        lines.append(_live_line(s))
    ledger = _ledger(home)
    try:
        c = ledger.counts()
        queue = f"{c['pending']} queued" + (f" ({c['in_flight']} sending now)" if c["in_flight"] else "")
        lines.append(f"  this install  {c['receipts']} stored  ·  {queue}  ·  {c['failed']} failed")
        rows = ledger.receipts(proofs)
        if rows:
            lines.append("  latest proofs")
            lines += [f"    ✓ {core.clip(r['text'], 60):<62} {core.proof_url(r['blob_id'])}" for r in rows]
        for f in ledger.failed(3):
            lines.append(f"    ✗ {f['kind']} write failed: {core.clip(f['last_error'] or '', 80)}  (/tusk retry)")
    finally:
        ledger.close()
    return "\n".join(lines)


def _live_line(s: core.Settings) -> str:
    try:
        client = core.make_client(s)
    except Exception as exc:
        return f"  relayer     unavailable: {exc}"
    try:
        spaces = client.list_namespaces().namespaces
        total = next((n.memory_count for n in spaces if n.name == s.namespace), 0)
        return f"  on Walrus   {total} memories in '{s.namespace}' across all your machines and agents"
    except Exception as exc:
        return f"  relayer     unreachable ({s.server_url}): {core.clip(str(exc), 120)}"
    finally:
        client.close()


def proofs_report(home: Path | None = None, limit: int = 10) -> str:
    ledger = _ledger(home)
    try:
        rows = ledger.receipts(limit)
    finally:
        ledger.close()
    if not rows:
        return (
            "No memories stored from this install yet. Memories written on other machines are still "
            "recalled; see them with proofs via: /tusk recall <topic>"
        )
    lines = [f"Last {len(rows)} memories this install stored on Walrus (each is a public blob):"]
    for r in rows:
        lines.append(f"  ✓ {core.clip(r['text'], 90)}")
        lines.append(f"    {core.proof_url(r['blob_id'])}   {core.ago(r['created_at'])}")
    return "\n".join(lines)


def recall_report(query: str, home: Path | None = None, limit: int = 6, namespace: str | None = None) -> str:
    s = core.load_settings(home)
    if not s.configured:
        return "Tusk is not configured: run hermes memory setup"
    if not query.strip():
        return "Usage: recall <query>"
    client = core.make_client(s)
    try:
        hits = core.recall(client, query, s, limit, namespace)
    finally:
        client.close()
    ns = namespace or s.namespace
    if not hits:
        return f"Nothing in '{ns}' matches: {query}"
    lines = [f"From Walrus ('{ns}') for: {query}"]
    for h in hits:
        lines.append(f"  {h.relevance:.2f}  {core.clip(h.text, 90)}")
        if h.blob_id:
            lines.append(f"        {core.proof_url(h.blob_id)}")
    return "\n".join(lines)


def verify_report(home: Path | None = None, limit: int = 3, aggregator: str = core.DEFAULT_AGGREGATOR) -> str:
    """Download recent blobs straight from a public Walrus aggregator and check they are ciphertext."""
    ledger = _ledger(home)
    try:
        rows = ledger.receipts(limit)
    finally:
        ledger.close()
    if not rows:
        return "Nothing to verify yet: this install has not stored a memory."
    lines = [f"Fetching {len(rows)} blobs directly from Walrus mainnet ({aggregator}), no relayer, no key:"]
    for r in rows:
        t0 = time.time()
        res = core.verify_blob(r["blob_id"], aggregator)
        took = f"{time.time() - t0:.1f}s"
        if not res["ok"]:
            lines.append(f"  ✗ {core.short_blob(r['blob_id'])}  not retrievable (HTTP {res['status']}) {took}")
            continue
        sealed = core.plaintext_absent(res["body"], r["text"])
        verdict = {
            True: "encrypted: none of the remembered words appear",
            False: "WARNING: plaintext visible",
            None: "stored (text too short to compare)",
        }[sealed]
        lines.append(f"  ✓ {core.short_blob(r['blob_id'])}  {res['bytes']} bytes on Walrus  ·  {verdict}  ({took})")
        lines.append(f"      remembered: “{core.clip(r['text'], 64)}”")
        lines.append(f"      stored:     {core.hex_preview(res['body'], 20)} …")
    return "\n".join(lines)


HELP = """/tusk                    status and latest proofs
/tusk proofs [n]         last n memories with Walruscan proof links
/tusk recall <query>     semantic search across all your machines and agents
/tusk verify [n]         download the last n blobs from Walrus and check they are encrypted
/tusk retry              re-queue writes that failed"""


def tusk_command(raw_args: str = "", **_context: Any) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        parts = (raw_args or "").split()
    sub, rest = (parts[0].lower(), parts[1:]) if parts else ("status", [])
    home = active_home()
    actions = {
        "status": lambda: status_report(home),
        "proofs": lambda: proofs_report(home, limit=_count(rest, 10)),
        "recall": lambda: recall_report(" ".join(rest), home),
        "verify": lambda: verify_report(home, limit=_count(rest, 3)),
        "retry": lambda: _retry(home),
    }
    action = actions.get({"proof": "proofs", "search": "recall"}.get(sub, sub))
    if action is None:
        return HELP
    try:
        return action()
    except Exception as exc:
        return f"tusk: {type(exc).__name__}: {exc}"


def _retry(home: Path | None) -> str:
    ledger = _ledger(home)
    try:
        n = ledger.requeue_failed()
    finally:
        ledger.close()
    return f"Re-queued {n} failed write(s); the writer picks them up within seconds."


def _count(rest: list[str], default: int) -> int:
    try:
        return max(1, min(50, int(rest[0])))
    except (IndexError, ValueError):
        return default
