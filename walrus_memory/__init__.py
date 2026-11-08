"""Walrus Memory for Hermes Agent.

A memory provider that keeps Hermes' long-term memory on Walrus mainnet (decentralized
storage on Sui) through the official Walrus Memory SDK. Facts are extracted and encrypted
by the Walrus Memory relayer (inside a TEE, sealed with SEAL) and stored as Walrus blobs
owned by the user's account, so the memory survives a reinstall, follows the user to any
machine and can be shared with other agents that use the same account and namespace.

Surfaces used:
  * memory provider  - prefetch recall, per-turn capture, mirrors built-in memory writes,
                        saves insights before context compression
  * tools            - walrus_memory_search / walrus_memory_remember / walrus_memory_status
  * slash command    - /walrus  (status, proofs, recall, verify, retry)
  * CLI              - hermes walrus-memory  (status, recall, proofs, verify, import, flush, restore)
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from . import walrus_core as core

logger = logging.getLogger("walrus-memory")

try:  # inside Hermes
    from agent.memory_provider import MemoryProvider as _Base
    from agent.memory_provider import RecallStatus, is_trivial_prompt, spawn_context_thread
except ImportError:  # pragma: no cover - standalone import (docs, linters)
    _Base = object  # type: ignore[assignment,misc]
    RecallStatus = None  # type: ignore[assignment]

    def is_trivial_prompt(text):  # type: ignore[no-redef]
        return not (text or "").strip()

    def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):  # type: ignore[no-redef]
        return threading.Thread(target=target, name=name, daemon=daemon, args=args, kwargs=kwargs)


TOOL_SEARCH = "walrus_memory_search"
TOOL_REMEMBER = "walrus_memory_remember"
TOOL_STATUS = "walrus_memory_status"

TOOL_SCHEMAS = [
    {
        "name": TOOL_SEARCH,
        "description": (
            "Semantic search over the user's long-term memory stored on Walrus. Returns matching "
            "memories with a relevance score and a public Walruscan proof link for each one. Use it "
            "when the automatically recalled memories are not enough, or when the user asks where a "
            "fact came from."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in natural language."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Max results (default 6)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": TOOL_REMEMBER,
        "description": (
            "Store one durable fact in the user's long-term memory on Walrus (decentralized, encrypted, "
            "owned by the user, available on every machine and to their other agents). Use for stable "
            "preferences, decisions, project facts and names. Do not store secrets or one-off chatter. "
            "The write is queued and lands on Walrus within a few seconds to a minute."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "A short, self-contained fact, e.g. 'The user deploys with Fly.io'."},
            },
            "required": ["fact"],
        },
    },
    {
        "name": TOOL_STATUS,
        "description": "Walrus Memory status: namespace, writes waiting in the queue, and the latest stored memories with proof links.",
        "parameters": {"type": "object", "properties": {}},
    },
]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class WalrusMemoryProvider(_Base):
    """Long-term memory on Walrus mainnet via the official Walrus Memory SDK."""

    def __init__(self, client: Any = None, settings: Optional[core.Settings] = None,
                 write_gap_s: float = core.WRITE_GAP_S) -> None:
        self._client = client
        self._settings = settings
        self._write_gap_s = write_gap_s
        self._hermes_home: Optional[Path] = None
        self._ledger: Optional[core.Ledger] = None
        self._writer: Optional[core.Writer] = None
        self._writes_enabled = True
        self._client_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: dict[str, tuple[str, list[core.Hit]]] = {}
        self._last_count: Optional[int] = None
        self._prefetch_threads: dict[str, threading.Thread] = {}
        self._explicit_facts_this_turn = False

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return core.PLUGIN_NAME

    def is_available(self) -> bool:
        """Credentials present and SDK importable. No network."""
        settings = self._settings or core.load_settings(None)
        if not settings.configured:
            return False
        try:
            import memwal  # noqa: F401
        except ImportError:
            return False
        return True

    def unavailable_reason(self) -> str:
        settings = self._settings or core.load_settings(None)
        if not settings.configured:
            return (f"Set {core.ENV_KEY} and {core.ENV_ACCOUNT} (from https://memory.walrus.xyz) "
                    "with `hermes memory setup`.")
        try:
            import memwal  # noqa: F401
        except ImportError:
            return "The memwal Python package is missing; reinstall the plugin so its dependencies install."
        return ""

    # ------------------------------------------------------------------ lifecycle

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        home = kwargs.get("hermes_home")
        self._hermes_home = Path(home) if home else None
        if self._settings is None:
            self._settings = core.load_settings(self._hermes_home)
        # Subagents, cron runs and flush forks never write; the primary agent owns the memory.
        self._writes_enabled = (kwargs.get("agent_context") or "primary") == "primary"
        self._ledger = core.open_ledger(self._hermes_home)
        self._writer = core.Writer(self._ledger, self._get_client, gap_s=self._write_gap_s,
                                   spawn=spawn_context_thread)
        if self._writes_enabled:
            self._writer.start()  # resumes anything left in the outbox by a previous run

    def shutdown(self) -> None:
        if self._writer:
            self._writer.stop(timeout=2.0)  # unsent writes stay in the outbox for next start
        client, self._client = self._client, None
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        if self._ledger:
            self._ledger.close()
            self._ledger = None

    def _get_client(self):
        with self._client_lock:
            if self._client is None:
                self._client = core.make_client(self._settings)
            return self._client

    # ------------------------------------------------------------------ prompt

    def system_prompt_block(self) -> str:
        ns = self._settings.namespace if self._settings else core.DEFAULT_NAMESPACE
        return (
            f"Walrus Memory is active (namespace '{ns}'): the user's long-term memory lives on Walrus, "
            "decentralized and encrypted, owned by the user and shared across their machines and agents. "
            "Relevant memories are recalled automatically each turn inside <walrus-memory> tags, and "
            "finished turns are distilled into facts automatically. Entries you save with the built-in "
            "memory tool are mirrored to Walrus too, so save a fact once, with either tool, never both. "
            f"Use {TOOL_SEARCH} to look things up with proof links."
        )

    # ------------------------------------------------------------------ recall

    def _recall(self, query: str, limit: Optional[int] = None) -> list[core.Hit]:
        return core.recall(self._get_client(), query, self._settings, limit)

    def _recall_bounded(self, query: str) -> Optional[list[core.Hit]]:
        """Live recall capped at prefetch_timeout_s; None on timeout or error."""
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["hits"] = self._recall(query)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc

        t = spawn_context_thread(run, name="walrus-memory-recall")
        t.start()
        t.join(self._settings.prefetch_timeout_s)
        if "error" in box:
            logger.warning("walrus-memory recall failed: %s", box["error"])
        return box.get("hits")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._settings or not self._settings.configured or not (query or "").strip() \
                or is_trivial_prompt(query):
            self._last_count = None
            return ""
        with self._cache_lock:
            cached = self._cache.get(session_id)
        if cached and cached[0] == query:
            hits = cached[1]  # same turn, later API call: reuse
        else:
            hits = self._recall_bounded(query)
            if hits is None:  # slow network: fall back to what queue_prefetch warmed
                hits = cached[1] if cached else []
            else:
                with self._cache_lock:
                    self._cache[session_id] = (query, hits)
        self._last_count = len(hits)
        return core.format_recall(hits, self._settings.namespace)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._settings or not self._settings.configured or not (query or "").strip():
            return
        previous = self._prefetch_threads.get(session_id)
        if previous and previous.is_alive():
            return

        def warm() -> None:
            try:
                hits = self._recall(query)
            except Exception as exc:  # noqa: BLE001
                logger.debug("walrus-memory background recall failed: %s", exc)
                return
            with self._cache_lock:
                self._cache[session_id] = (query, hits)

        t = spawn_context_thread(warm, name="walrus-memory-prefetch")
        self._prefetch_threads[session_id] = t
        t.start()

    def recall_status(self):
        if self._last_count is None or RecallStatus is None:
            return None
        return RecallStatus(provider_label="Walrus", count=self._last_count, glyph="🦭")

    # ------------------------------------------------------------------ capture

    def _enqueue(self, kind: str, text: str) -> bool:
        if not self._writes_enabled or self._ledger is None or not self._settings:
            return False
        added = self._ledger.enqueue(kind, self._settings.namespace, text)
        if kind == "fact":
            self._explicit_facts_this_turn = True
        if added and self._writer:
            self._writer.wake()
        return added

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: Any = None) -> None:
        """Queue the finished turn for fact extraction. Non-blocking: one SQLite insert."""
        explicit, self._explicit_facts_this_turn = self._explicit_facts_this_turn, False
        if explicit:
            return  # facts were already saved explicitly this turn; extracting again would duplicate them
        if not self._settings or not self._settings.capture_turns:
            return
        if not (user_content or "").strip() or is_trivial_prompt(user_content):
            return
        text = (f"User: {core.clip(user_content, core.MAX_TURN_CHARS)}\n"
                f"Assistant: {core.clip(assistant_content, core.MAX_TURN_CHARS)}")
        self._enqueue("turn", text)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[dict[str, Any]] = None) -> None:
        """Mirror Hermes' built-in memory (MEMORY.md / USER.md) onto Walrus."""
        if action not in ("add", "replace") or not (content or "").strip():
            return
        label = "About the user" if target == "user" else "Note"
        self._enqueue("fact", f"{label}: {core.clip(content, core.MAX_FACT_CHARS)}")

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Before context is compressed away, send the dropped dialogue for fact extraction."""
        parts: list[str] = []
        for m in messages[-24:]:
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                parts.append(f"{role.title()}: {core.clip(content, 600)}")
        if parts:
            self._enqueue("turn", core.clip("\n".join(parts), 6000))
        return ""

    # ------------------------------------------------------------------ tools

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            if tool_name == TOOL_SEARCH:
                query = str(args.get("query") or "").strip()
                if not query:
                    return _json({"error": "query is required"})
                limit = max(1, min(20, int(args.get("limit") or self._settings.recall_limit)))
                hits = self._recall(query, limit)
                return _json({"namespace": self._settings.namespace, "count": len(hits),
                              "results": [h.as_dict() for h in hits]})
            if tool_name == TOOL_REMEMBER:
                fact = str(args.get("fact") or args.get("text") or "").strip()
                if not fact:
                    return _json({"error": "fact is required"})
                if not self._writes_enabled:
                    return _json({"queued": False, "reason": "memory writes are disabled in this agent context"})
                added = self._enqueue("fact", core.clip(fact, core.MAX_FACT_CHARS))
                counts = self._ledger.counts() if self._ledger else {}
                return _json({"queued": added, "duplicate": not added, "pending_writes": counts.get("pending", 0),
                              "note": "Queued for Walrus. Proof link appears in /walrus once stored."})
            if tool_name == TOOL_STATUS:
                return _json(self.status())
        except Exception as exc:  # noqa: BLE001
            return _json({"error": f"{type(exc).__name__}: {exc}"})
        return _json({"error": f"unknown tool {tool_name}"})

    def status(self) -> dict[str, Any]:
        counts = self._ledger.counts() if self._ledger else {}
        recent = self._ledger.receipts(5) if self._ledger else []
        return {
            "namespace": self._settings.namespace if self._settings else None,
            "account": self._settings.account_short if self._settings else None,
            "stored_by_this_install": counts.get("receipts", 0),
            "pending_writes": counts.get("pending", 0),
            "failed_writes": counts.get("failed", 0),
            "recent": [{"text": r["text"], "blob_id": r["blob_id"], "proof_url": core.proof_url(r["blob_id"])}
                       for r in recent],
        }

    # ------------------------------------------------------------------ setup

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "private_key", "description": "Walrus Memory delegate private key (hex)",
             "secret": True, "required": True, "env_var": core.ENV_KEY, "url": "https://memory.walrus.xyz"},
            {"key": "account_id", "description": "Walrus Memory account id (0x...)",
             "secret": True, "required": True, "env_var": core.ENV_ACCOUNT, "url": "https://memory.walrus.xyz"},
            {"key": "namespace", "description": "Memory namespace (use the same one as your other agents to share memory)",
             "default": core.DEFAULT_NAMESPACE},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        path = core.config_path(Path(hermes_home))
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        for key in ("namespace", "server_url", "capture_turns", "recall_limit", "max_distance", "prefetch_timeout_s"):
            if key in values and values[key] not in (None, ""):
                current[key] = values[key]
        path.write_text(json.dumps(current, indent=2), encoding="utf-8")


def register(ctx) -> None:
    ctx.register_memory_provider(WalrusMemoryProvider())
    from .commands import walrus_command

    ctx.register_command(
        "walrus",
        walrus_command,
        description="Walrus Memory: status, proofs, recall, verify",
        args_hint="[status|proofs|recall <query>|verify|retry]",
    )
