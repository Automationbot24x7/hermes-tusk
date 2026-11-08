"""Tusk: long-term memory for Hermes Agent, stored on Walrus mainnet.

Tusk is a memory provider built on the official Walrus Memory SDK. The Walrus Memory
relayer extracts facts and encrypts them inside its TEE (SEAL), then stores each one as
a Walrus blob owned by the user's account. The memory therefore survives a reinstall,
follows the user to any machine, and can be shared with other agents on the same account.

Hermes surfaces used:
  * memory provider - recall before each turn, turn capture, mirroring of built-in memory
                      writes, a save before context compression
  * tools           - tusk_search, tusk_remember, tusk_namespaces, tusk_status
  * slash command   - /tusk
  * CLI             - hermes tusk (cli.py)
  * dashboard tab   - dashboard/ (memory explorer with on-chain verification)
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import core, outbox

logger = logging.getLogger("tusk")

try:
    from agent.memory_provider import (
        MemoryProvider,
        RecallStatus,
        is_trivial_prompt,
        spawn_context_thread,
    )
except ImportError:  # imported outside Hermes (docs tooling); the provider is unusable there anyway
    MemoryProvider = object  # type: ignore[assignment,misc]
    RecallStatus = None  # type: ignore[assignment]

    def is_trivial_prompt(text: str | None) -> bool:  # type: ignore[misc]
        return not (text or "").strip()

    def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):  # type: ignore[misc]
        return threading.Thread(target=target, name=name, daemon=daemon, args=args, kwargs=kwargs)


TOOL_SEARCH = "tusk_search"
TOOL_REMEMBER = "tusk_remember"
TOOL_NAMESPACES = "tusk_namespaces"
TOOL_STATUS = "tusk_status"

_NAMESPACE_PARAM = {
    "type": "string",
    "description": "Optional namespace, e.g. one another agent writes to. Defaults to the configured one.",
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": TOOL_SEARCH,
        "description": (
            "Semantic search over the user's long-term memory on Walrus. Returns matching memories with a "
            "relevance score and a public Walruscan proof link each. Use it when the memories recalled "
            "automatically are not enough, to look in another namespace, or when the user asks where a "
            "fact came from."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in natural language."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Max results (default 6)."},
                "namespace": _NAMESPACE_PARAM,
            },
            "required": ["query"],
        },
    },
    {
        "name": TOOL_REMEMBER,
        "description": (
            "Store a durable fact in the user's long-term memory on Walrus: encrypted, owned by the user, "
            "available on all their machines and to their other agents. Use it for stable preferences, "
            "decisions, names and project facts; never for secrets or passing chatter. If you save facts "
            "explicitly in a turn, that turn is not auto-extracted, so save everything worth keeping."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "One short, self-contained fact."},
                "namespace": _NAMESPACE_PARAM,
            },
            "required": ["fact"],
        },
    },
    {
        "name": TOOL_NAMESPACES,
        "description": "List the memory namespaces on the user's Walrus Memory account with their memory counts.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": TOOL_STATUS,
        "description": "Tusk status: namespace, queued and failed writes, latest stored memories with proof links.",
        "parameters": {"type": "object", "properties": {}},
    },
]

_CACHE_SESSIONS = 64
# Recalled once per process, in the background, and added to the first turn of every session, so a
# broad opener ("what do you know about me?") still gets the user's core facts.
PROFILE_QUERY = "about the user: name, role, location, preferences, routines and current projects"
PROFILE_LIMIT = 4
WARMUP_WAIT_S = 5.0  # the first turn may wait this long for the warm-up (Hermes allows 8 s for recall)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _text_of(content: Any) -> str:
    """Text of an OpenAI-style message content: a string, or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


class TuskMemoryProvider(MemoryProvider):
    """Hermes long-term memory on Walrus mainnet."""

    def __init__(
        self, client: Any = None, settings: core.Settings | None = None, write_gap_s: float = outbox.WRITE_GAP_S
    ) -> None:
        self._client = client
        self._settings = settings
        self._write_gap_s = write_gap_s
        self._hermes_home: Path | None = None
        self._ledger: outbox.Ledger | None = None
        self._writer: outbox.Writer | None = None
        self._writes_enabled = False
        self._client_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[str, list[core.Hit]]] = OrderedDict()
        self._warming: dict[str, threading.Thread] = {}
        self._last_count: int | None = None
        self._explicit_this_turn = False
        self._warmup: threading.Thread | None = None
        self._profile: list[core.Hit] = []
        self._sessions_seen: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------------ availability

    @property
    def name(self) -> str:
        return core.PLUGIN_NAME

    def _resolved_settings(self) -> core.Settings:
        return self._settings or core.load_settings(None)

    def is_available(self) -> bool:
        """Credentials present and the SDK importable. No network."""
        return not self.unavailable_reason()

    def unavailable_reason(self) -> str:
        if not self._resolved_settings().configured:
            return (
                f"Tusk needs {core.ENV_KEY} and {core.ENV_ACCOUNT} from {core.SIGNUP_URL}; "
                "run `hermes memory setup` and pick tusk."
            )
        try:
            import memwal  # noqa: F401
        except ImportError:
            return "The memwal package is missing; reinstall Tusk so its Python dependencies install."
        return ""

    # ------------------------------------------------------------------ lifecycle

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        if self._ledger is not None:
            self.shutdown()  # re-initialised: drop the previous binding first
        home = kwargs.get("hermes_home")
        self._hermes_home = Path(home) if home else None
        if self._settings is None:
            self._settings = core.load_settings(self._hermes_home)
        # The primary agent owns the memory: subagents, cron runs and flush forks only read.
        self._writes_enabled = (kwargs.get("agent_context") or "primary") == "primary"
        self._ledger = outbox.open_ledger(core.data_dir(self._hermes_home))
        self._writer = outbox.Writer(
            self._ledger, self._get_client, gap_s=self._write_gap_s, spawn=spawn_context_thread
        )
        if self._writes_enabled:
            self._writer.start()  # also resumes anything an earlier run left queued
        if self._settings.configured:
            # Connect and fetch the user's profile facts while Hermes is still starting up, so the
            # first turn neither pays the connection cost nor misses the basics.
            self._warmup = spawn_context_thread(self._warm_up, name="tusk-warmup")
            self._warmup.start()

    def _warm_up(self) -> None:
        try:
            self._profile = self._recall(PROFILE_QUERY, PROFILE_LIMIT)
        except Exception as exc:
            logger.debug("tusk: warm-up recall failed: %s", exc)

    def shutdown(self) -> None:
        writer, ledger, client = self._writer, self._ledger, self._client
        self._writer = self._ledger = None
        stopped = writer.stop(timeout=2.0) if writer else True
        if not stopped:
            return  # a write is mid-flight: leave its ledger and client to the daemon thread
        self._client = None
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception as exc:
                logger.debug("tusk: closing client: %s", exc)
        if ledger is not None:
            ledger.close()

    def _get_client(self) -> Any:
        with self._client_lock:
            if self._client is None:
                self._client = core.make_client(self._settings)
            return self._client

    # ------------------------------------------------------------------ prompt

    def system_prompt_block(self) -> str:
        ns = self._settings.namespace if self._settings else core.DEFAULT_NAMESPACE
        return (
            f"Tusk memory is active (namespace '{ns}'). The user's long-term memory lives on Walrus: "
            "encrypted, owned by the user, shared across their machines and agents. Relevant memories are "
            "recalled into <tusk-memory> blocks before each turn, finished turns are distilled into facts "
            "automatically, and entries saved with the built-in memory tool are mirrored to Walrus. Save a "
            f"fact once, with either tool. Use {TOOL_SEARCH} for deeper lookups and proof links."
        )

    # ------------------------------------------------------------------ recall

    def _recall(self, query: str, limit: int | None = None, namespace: str | None = None) -> list[core.Hit]:
        return core.recall(self._get_client(), query, self._settings, limit, namespace)

    def _remember_cache(self, session_id: str, query: str, hits: list[core.Hit]) -> None:
        with self._cache_lock:
            self._cache[session_id] = (query, hits)
            self._cache.move_to_end(session_id)
            while len(self._cache) > _CACHE_SESSIONS:
                self._cache.popitem(last=False)

    def _recall_bounded(self, query: str, session_id: str) -> list[core.Hit] | None:
        """Live recall that waits at most prefetch_timeout_s. A late answer still lands in
        the cache, so the next API call of the same turn gets it."""
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["hits"] = self._recall(query)
                self._remember_cache(session_id, query, box["hits"])
            except Exception as exc:
                logger.warning("tusk: recall failed: %s", exc)

        thread = spawn_context_thread(run, name="tusk-recall")
        thread.start()
        thread.join(self._settings.prefetch_timeout_s)
        return box.get("hits")

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._settings or not self._settings.configured or is_trivial_prompt(query):
            self._last_count = None
            return ""
        first_turn = session_id not in self._sessions_seen
        if first_turn:
            self._sessions_seen[session_id] = None
            while len(self._sessions_seen) > _CACHE_SESSIONS:
                self._sessions_seen.popitem(last=False)
            if self._warmup is not None:
                self._warmup.join(WARMUP_WAIT_S)
        with self._cache_lock:
            cached = self._cache.get(session_id)
        if cached and cached[0] == query:
            hits = cached[1]  # a later API call within the same turn
        else:
            hits = self._recall_bounded(query, session_id)
            if hits is None:  # slow network: use what queue_prefetch warmed after the last turn
                hits = cached[1] if cached else []
        if first_turn:
            known = {h.blob_id for h in hits}
            hits = hits + [h for h in self._profile if h.blob_id not in known]
        self._last_count = len(hits) or None
        return core.format_recall(hits, self._settings.namespace)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._settings or not self._settings.configured or not (query or "").strip():
            return
        self._warming = {sid: t for sid, t in self._warming.items() if t.is_alive()}
        if session_id in self._warming:
            return

        def warm() -> None:
            try:
                self._remember_cache(session_id, query, self._recall(query))
            except Exception as exc:
                logger.debug("tusk: background recall failed: %s", exc)

        thread = spawn_context_thread(warm, name="tusk-prefetch")
        self._warming[session_id] = thread
        thread.start()

    def recall_status(self):
        if self._last_count is None or RecallStatus is None:
            return None
        return RecallStatus(provider_label="Tusk", count=self._last_count, glyph="🦭")

    # ------------------------------------------------------------------ capture

    def _enqueue(self, kind: str, text: str, namespace: str | None = None) -> bool:
        if not self._writes_enabled or self._ledger is None:
            return False
        added = self._ledger.enqueue(kind, namespace or self._settings.namespace, core.redact(text))
        if added and self._writer:
            self._writer.wake()
        return added

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._explicit_this_turn = False

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = "", messages: Any = None
    ) -> None:
        """Queue the finished turn for fact extraction: a single SQLite insert, never blocks."""
        if self._explicit_this_turn:
            return  # facts were saved explicitly this turn; extracting the turn again duplicates them
        if not self._settings or not self._settings.capture_turns or is_trivial_prompt(user_content):
            return
        self._enqueue(
            "turn",
            f"User: {core.clip(user_content, core.MAX_TURN_CHARS)}\n"
            f"Assistant: {core.clip(assistant_content, core.MAX_TURN_CHARS)}",
        )

    def on_memory_write(self, action: str, target: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Mirror Hermes' built-in memory (MEMORY.md / USER.md) onto Walrus."""
        if action not in ("add", "replace") or not (content or "").strip():
            return
        label = "About the user" if target == "user" else "Note"
        if self._enqueue("fact", f"{label}: {core.clip(content, core.MAX_FACT_CHARS)}"):
            self._explicit_this_turn = True

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Send dialogue that is about to be compressed away for fact extraction."""
        parts = []
        for m in messages[-24:]:
            text = _text_of(m.get("content")).strip()
            if m.get("role") in ("user", "assistant") and text:
                parts.append(f"{m['role'].title()}: {core.clip(text, 600)}")
        if parts:
            self._enqueue("turn", core.clip("\n".join(parts), 6000))
        return ""

    # ------------------------------------------------------------------ tools

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            if tool_name == TOOL_SEARCH:
                return self._tool_search(args)
            if tool_name == TOOL_REMEMBER:
                return self._tool_remember(args)
            if tool_name == TOOL_NAMESPACES:
                spaces = self._get_client().list_namespaces().namespaces
                return _json(
                    {
                        "current": self._settings.namespace,
                        "namespaces": [{"name": n.name, "memories": n.memory_count} for n in spaces],
                    }
                )
            if tool_name == TOOL_STATUS:
                return _json(self.snapshot())
        except Exception as exc:
            return _json({"error": f"{type(exc).__name__}: {exc}"})
        return _json({"error": f"unknown tool {tool_name}"})

    def _tool_search(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return _json({"error": "query is required"})
        try:
            limit = max(1, min(20, int(args.get("limit") or self._settings.recall_limit)))
        except (TypeError, ValueError):
            return _json({"error": "limit must be an integer"})
        namespace = str(args.get("namespace") or "").strip() or self._settings.namespace
        hits = self._recall(query, limit, namespace)
        return _json({"namespace": namespace, "count": len(hits), "results": [h.as_dict() for h in hits]})

    def _tool_remember(self, args: dict[str, Any]) -> str:
        fact = str(args.get("fact") or "").strip()
        if not fact:
            return _json({"error": "fact is required"})
        if not self._writes_enabled:
            return _json({"queued": False, "reason": "this agent context only reads memory"})
        namespace = str(args.get("namespace") or "").strip() or self._settings.namespace
        added = self._enqueue("fact", core.clip(fact, core.MAX_FACT_CHARS), namespace)
        self._explicit_this_turn = True
        return _json(
            {
                "queued": added,
                "duplicate": not added,
                "namespace": namespace,
                "pending_writes": self._ledger.counts()["pending"],
                "note": "Queued for Walrus; its proof link appears in /tusk once stored.",
            }
        )

    def snapshot(self) -> dict[str, Any]:
        counts = self._ledger.counts() if self._ledger else {}
        recent = self._ledger.receipts(5) if self._ledger else []
        return {
            "namespace": self._settings.namespace if self._settings else None,
            "account": self._settings.account_short if self._settings else None,
            "stored_by_this_install": counts.get("receipts", 0),
            "pending_writes": counts.get("pending", 0),
            "failed_writes": counts.get("failed", 0),
            "recent": [{"text": r["text"], "proof_url": core.proof_url(r["blob_id"])} for r in recent],
        }

    # ------------------------------------------------------------------ setup

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "private_key",
                "description": "Walrus Memory delegate private key (hex)",
                "secret": True,
                "required": True,
                "env_var": core.ENV_KEY,
                "url": core.SIGNUP_URL,
            },
            {
                "key": "account_id",
                "description": "Walrus Memory account id (0x...)",
                "secret": True,
                "required": True,
                "env_var": core.ENV_ACCOUNT,
                "url": core.SIGNUP_URL,
            },
            {
                "key": "namespace",
                "description": "Namespace; use the one your other agents use to share memory",
                "default": core.DEFAULT_NAMESPACE,
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        path = core.config_paths(Path(hermes_home))[1]
        current = core.read_options(Path(hermes_home))
        current.update({k: v for k, v in values.items() if k in core.OPTION_KEYS and v not in (None, "")})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2), encoding="utf-8")


def register(ctx) -> None:
    from .commands import tusk_command

    ctx.register_memory_provider(TuskMemoryProvider())
    ctx.register_command(
        "tusk",
        tusk_command,
        description="Tusk memory on Walrus: status, proofs, recall, verify",
        args_hint="[status|proofs|recall <query>|verify|retry]",
    )
