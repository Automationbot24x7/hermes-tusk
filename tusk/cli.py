"""`hermes tusk ...` subcommands (shown while tusk is the active memory provider)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import commands
from . import walrus_core as core

ENTRY_DELIMITER = "\n§\n"  # Hermes' built-in MEMORY.md / USER.md entry separator


def _home() -> Optional[Path]:
    return commands._hermes_home()


def _print(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def cmd_status(args: Any) -> int:
    _print(commands.status_report(_home(), live=not args.offline, receipts=args.limit))
    return 0


def cmd_recall(args: Any) -> int:
    _print(commands.recall_report(" ".join(args.query), _home(), limit=args.limit))
    return 0


def cmd_proofs(args: Any) -> int:
    _print(commands.proofs_report(_home(), limit=args.limit))
    return 0


def cmd_verify(args: Any) -> int:
    _print(commands.verify_report(_home(), limit=args.limit, aggregator=args.aggregator))
    return 0


def cmd_retry(args: Any) -> int:
    ledger = core.open_ledger(_home())
    try:
        _print(f"Re-queued {ledger.requeue_failed()} failed write(s).")
    finally:
        ledger.close()
    return 0


def flush(home: Optional[Path], settings: core.Settings, gap_s: float = core.WRITE_GAP_S,
          client: Any = None, out=_print) -> int:
    """Drain the outbox now, printing a proof link per stored memory. Returns memories stored."""
    ledger = core.open_ledger(home)
    own_client = client is None
    client = client or core.make_client(settings)
    writer = core.Writer(ledger, lambda: client, gap_s=gap_s)
    stored = 0
    try:
        total = ledger.counts()["pending"]
        if not total:
            out("Queue is empty: everything is already on Walrus.")
            return 0
        out(f"Uploading {total} queued write(s) to Walrus (paced for the relayer's rate limit)...")
        idle = 0
        while ledger.counts()["pending"] and idle < 3:
            result = writer.step()
            if result is None:  # due later (backoff) or leased by a running Hermes
                idle += 1
                time.sleep(2)
                continue
            idle = 0
            if result["ok"]:
                for text, blob_id in result["receipts"]:
                    stored += 1
                    out(f"  ✓ {core.clip(text, 70):<72} {core.proof_url(blob_id)}")
                if not result["receipts"]:
                    out("  · nothing durable to extract from that turn")
            else:
                out(f"  ! {result['kind']} write {result['status']}: {core.clip(result['error'], 120)}")
        left = ledger.counts()
        out(f"Done: {stored} stored, {left['pending']} still queued, {left['failed']} failed.")
    finally:
        ledger.close()
        if own_client:
            client.close()
    return stored


def cmd_flush(args: Any) -> int:
    home = _home()
    s = core.load_settings(home)
    if not s.configured:
        _print("Tusk is not configured.")
        return 1
    flush(home, s)
    return 0


def read_builtin_entries(home: Path) -> list[str]:
    """Entries from Hermes' built-in memory files, labelled like on_memory_write does."""
    out: list[str] = []
    for fname, label in (("USER.md", "About the user"), ("MEMORY.md", "Note")):
        path = Path(home) / "memories" / fname
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for entry in raw.split(ENTRY_DELIMITER):
            entry = entry.strip()
            if entry and not entry.startswith("#"):
                out.append(f"{label}: {core.clip(entry, core.MAX_FACT_CHARS)}")
    return out


def cmd_import(args: Any) -> int:
    home = _home() or Path.home() / ".hermes"
    s = core.load_settings(home)
    entries = read_builtin_entries(home)
    if not entries:
        _print(f"No built-in memory entries found under {home / 'memories'}.")
        return 0
    ledger = core.open_ledger(home)
    try:
        added = sum(1 for e in entries if ledger.enqueue("fact", s.namespace, e))
    finally:
        ledger.close()
    _print(f"Queued {added} of {len(entries)} built-in memory entries for Walrus "
           f"({len(entries) - added} were already queued or stored).")
    if args.no_flush or not s.configured:
        _print("They upload in the background the next time Hermes runs (or run: hermes tusk flush).")
        return 0
    flush(home, s)
    return 0


def cmd_restore(args: Any) -> int:
    """Ask the relayer to rebuild this namespace's search index from the blobs on Walrus."""
    home = _home()
    s = core.load_settings(home)
    if not s.configured:
        _print("Tusk is not configured.")
        return 1
    ns = args.namespace or s.namespace
    client = core.make_client(s)
    try:
        _print(f"Rebuilding the '{ns}' index from Walrus blobs owned by {s.account_short}...")
        t0 = time.time()
        res = client.restore(ns, limit=args.limit)
        _print(f"  total blobs {res.total}  ·  restored {res.restored}  ·  already indexed {res.skipped}"
               f"  ·  failed {len(res.failed or []) if isinstance(res.failed, list) else res.failed}"
               f"  ({time.time() - t0:.1f}s)")
        if getattr(res, "truncated", False):
            _print(f"  more remain: run again with --limit {args.limit * 2}")
    finally:
        client.close()
    return 0


def register_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="tusk_command")

    p = subs.add_parser("status", help="Account, namespace, queue and latest proofs")
    p.add_argument("--offline", action="store_true", help="Skip the relayer health check")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(tusk_func=cmd_status)

    p = subs.add_parser("recall", help="Semantic search over your Walrus memory")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=6)
    p.set_defaults(tusk_func=cmd_recall)

    p = subs.add_parser("proofs", help="Latest stored memories with Walruscan proof links")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(tusk_func=cmd_proofs)

    p = subs.add_parser("verify", help="Fetch recent blobs straight from Walrus and check they are encrypted")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--aggregator", default=core.DEFAULT_AGGREGATOR)
    p.set_defaults(tusk_func=cmd_verify)

    p = subs.add_parser("import", help="Copy Hermes' built-in MEMORY.md / USER.md entries to Walrus")
    p.add_argument("--no-flush", action="store_true", help="Queue only; upload later in the background")
    p.set_defaults(tusk_func=cmd_import)

    p = subs.add_parser("flush", help="Upload everything still queued, now")
    p.set_defaults(tusk_func=cmd_flush)

    p = subs.add_parser("restore", help="Rebuild the namespace index from the blobs stored on Walrus")
    p.add_argument("--namespace", default=None)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(tusk_func=cmd_restore)

    p = subs.add_parser("retry", help="Re-queue writes that failed")
    p.set_defaults(tusk_func=cmd_retry)

    subparser.set_defaults(func=dispatch)


def dispatch(args: Any) -> int:
    fn = getattr(args, "tusk_func", None)
    if fn is None:  # bare `hermes tusk`
        args.offline, args.limit = False, 5
        fn = cmd_status
    return fn(args)
