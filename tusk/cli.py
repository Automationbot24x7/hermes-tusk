"""`hermes tusk ...`: available while tusk is the active memory provider."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import commands, core, outbox

ENTRY_DELIMITER = "\n§\n"  # separator Hermes uses between entries in MEMORY.md / USER.md


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _configured(home: Path | None) -> core.Settings | None:
    s = core.load_settings(home)
    if not s.configured:
        _say(f"Tusk is not configured. Get a delegate key at {core.SIGNUP_URL}, then run: hermes memory setup")
        return None
    return s


def flush(
    home: Path | None,
    settings: core.Settings,
    *,
    gap_s: float = outbox.WRITE_GAP_S,
    client: Any = None,
    out: Callable[[str], None] = _say,
) -> int:
    """Send everything queued now, printing a proof link per stored memory. Returns memories stored."""
    ledger = outbox.open_ledger(core.data_dir(home))
    own_client = client is None
    client = client or core.make_client(settings)
    writer = outbox.Writer(ledger, lambda: client, gap_s=gap_s)
    stored = 0
    try:
        pending = ledger.counts()["pending"]
        if not pending:
            out("Queue is empty: everything is already on Walrus.")
            return 0
        out(f"Sending {pending} queued write(s) to Walrus, paced for the relayer's rate limit...")
        while ledger.counts()["pending"]:
            result = writer.step()
            if result is None:
                wait = _next_due_in(ledger)
                if wait is None:  # everything left is being sent by a running Hermes
                    break
                if wait > 0:
                    out(f"  … waiting {wait:.0f}s for the relayer's rate limit")
                    time.sleep(min(wait, 90.0))
                continue
            if not result["ok"]:
                out(f"  ! {result['kind']} write {result['status']}: {core.clip(result['error'], 120)}")
            elif not result["receipts"]:
                out("  · nothing durable to extract from that turn")
            for text, blob_id in result.get("receipts") or []:
                stored += 1
                out(f"  ✓ {core.clip(text, 64):<66} {core.proof_url(blob_id)}")
        c = ledger.counts()
        out(f"Done: {stored} stored, {c['pending']} still queued, {c['failed']} failed.")
        if c["in_flight"]:
            out(f"  {c['in_flight']} write(s) are being sent by a running Hermes right now.")
    finally:
        ledger.close()
        if own_client:
            client.close()
    return stored


def _next_due_in(ledger: outbox.Ledger) -> float | None:
    """Seconds until the next queued job may be sent; None if every queued job is leased elsewhere."""
    now = time.time()
    free = [p for p in ledger.pending(1000) if p["claimed_until"] <= now]
    if not free:
        return None
    return max(0.0, min(p["next_at"] for p in free) - now)


def read_builtin_entries(home: Path) -> list[str]:
    """Entries of Hermes' built-in memory files, labelled the way on_memory_write labels them."""
    entries: list[str] = []
    for fname, label in (("USER.md", "About the user"), ("MEMORY.md", "Note")):
        try:
            raw = (Path(home) / "memories" / fname).read_text(encoding="utf-8")
        except OSError:
            continue
        for chunk in raw.split(ENTRY_DELIMITER):
            entry = chunk.strip()
            if entry and not entry.startswith("#"):
                entries.append(f"{label}: {core.clip(entry, core.MAX_FACT_CHARS)}")
    return entries


def export_receipts(home: Path | None, fmt: str) -> str:
    """Every memory this install stored, with its blob id and proof link (JSONL or Markdown)."""
    ledger = outbox.open_ledger(core.data_dir(home))
    try:
        rows = list(reversed(ledger.receipts(1_000_000)))
    finally:
        ledger.close()
    records = [
        {
            "text": r["text"],
            "namespace": r["namespace"],
            "blob_id": r["blob_id"],
            "proof_url": core.proof_url(r["blob_id"]),
            "stored_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["created_at"])),
        }
        for r in rows
    ]
    if fmt == "jsonl":
        return "\n".join(json.dumps(rec, ensure_ascii=False) for rec in records)
    lines = ["# Tusk memories on Walrus", "", "| Stored (UTC) | Namespace | Memory | Proof |", "|---|---|---|---|"]
    for rec in records:
        text = rec["text"].replace("|", "\\|").replace("\n", " ")
        proof = f"[{rec['blob_id'][:10]}]({rec['proof_url']})"
        lines.append(f"| {rec['stored_at']} | {rec['namespace']} | {text} | {proof} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- handlers


def cmd_status(args: Any) -> None:
    _say(commands.status_report(commands.active_home(), live=not args.offline, proofs=args.limit))


def cmd_recall(args: Any) -> None:
    _say(commands.recall_report(" ".join(args.query), commands.active_home(), args.limit, args.namespace))


def cmd_proofs(args: Any) -> None:
    _say(commands.proofs_report(commands.active_home(), limit=args.limit))


def cmd_verify(args: Any) -> None:
    try:
        core.check_aggregator(args.aggregator)
    except ValueError as exc:
        _say(str(exc))
        return
    _say(commands.verify_report(commands.active_home(), limit=args.limit, aggregator=args.aggregator))


def cmd_namespaces(args: Any) -> None:
    home = commands.active_home()
    s = _configured(home)
    if not s:
        return
    client = core.make_client(s)
    try:
        spaces = client.list_namespaces().namespaces
    finally:
        client.close()
    _say(f"Namespaces on account {s.account_short} (current: {s.namespace})")
    for n in sorted(spaces, key=lambda n: -n.memory_count):
        mark = "→" if n.name == s.namespace else " "
        _say(f"  {mark} {n.name:<28} {n.memory_count:>6} memories")


def cmd_flush(args: Any) -> None:
    home = commands.active_home()
    s = _configured(home)
    if s:
        flush(home, s)


def cmd_import(args: Any) -> None:
    home = commands.active_home() or Path.home() / ".hermes"
    s = core.load_settings(home)
    entries = read_builtin_entries(home)
    if not entries:
        _say(f"No built-in memory entries found in {home / 'memories'}.")
        return
    ledger = outbox.open_ledger(core.data_dir(home))
    try:
        added = sum(ledger.enqueue("fact", s.namespace, core.redact(e)) for e in entries)
    finally:
        ledger.close()
    _say(
        f"Queued {added} of {len(entries)} built-in memory entries for Walrus "
        f"({len(entries) - added} were already queued or sent)."
    )
    if args.no_flush or not s.configured:
        _say("They go out in the background next time Hermes runs, or now with: hermes tusk flush")
        return
    flush(home, s)


def cmd_export(args: Any) -> None:
    _say(export_receipts(commands.active_home(), args.format))


def cmd_restore(args: Any) -> None:
    home = commands.active_home()
    s = _configured(home)
    if not s:
        return
    namespace = args.namespace or s.namespace
    client = core.make_client(s)
    try:
        _say(f"Asking the relayer to rebuild the '{namespace}' index from blobs on Walrus ({s.account_short})...")
        t0 = time.time()
        res = client.restore(namespace, limit=args.limit)
        failed = len(res.failed) if isinstance(res.failed, list) else res.failed
        _say(
            f"  {res.total} blobs · {res.restored} restored · {res.skipped} already indexed · {failed} failed"
            f"  ({time.time() - t0:.1f}s)"
        )
        if res.truncated:
            _say(f"  More remain: run again with --limit {args.limit * 2}")
    finally:
        client.close()


def cmd_retry(args: Any) -> None:
    ledger = outbox.open_ledger(core.data_dir(commands.active_home()))
    try:
        _say(f"Re-queued {ledger.requeue_failed()} failed write(s).")
    finally:
        ledger.close()


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="tusk_command")

    def add(name: str, handler: Callable[[Any], None], help_text: str):
        p = subs.add_parser(name, help=help_text)
        p.set_defaults(tusk_handler=handler)
        return p

    p = add("status", cmd_status, "Account, namespace, queue and latest proofs")
    p.add_argument("--offline", action="store_true", help="Skip the relayer call")
    p.add_argument("--limit", type=int, default=5, help="Proofs to show")
    p = add("recall", cmd_recall, "Semantic search with relevance and proof links")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--namespace", default=None, help="Search another namespace")
    p = add("proofs", cmd_proofs, "Latest memories this install stored, with Walruscan links")
    p.add_argument("--limit", type=int, default=10)
    p = add("verify", cmd_verify, "Download recent blobs from Walrus and check they are encrypted")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--aggregator", default=core.DEFAULT_AGGREGATOR, help="Any public https aggregator")
    add("namespaces", cmd_namespaces, "Namespaces on your account and their memory counts")
    p = add("import", cmd_import, "Copy Hermes' built-in MEMORY.md / USER.md entries to Walrus")
    p.add_argument("--no-flush", action="store_true", help="Queue only; send later in the background")
    add("flush", cmd_flush, "Send everything still queued, now")
    p = add("export", cmd_export, "Print every stored memory with its blob id and proof link")
    p.add_argument("--format", choices=("md", "jsonl"), default="md")
    p = add("restore", cmd_restore, "Rebuild the namespace's search index from its blobs on Walrus")
    p.add_argument("--namespace", default=None)
    p.add_argument("--limit", type=int, default=50)
    add("retry", cmd_retry, "Re-queue writes that failed")
    subparser.set_defaults(func=dispatch)


def dispatch(args: Any) -> None:
    handler = getattr(args, "tusk_handler", None)
    if handler is None:  # bare `hermes tusk`
        args.offline, args.limit = False, 5
        handler = cmd_status
    handler(args)
