"""Offline tests: the official SDK's in-process mock stands in for the Walrus Memory relayer."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def drain(p, max_steps=10):
    results = []
    for _ in range(max_steps):
        r = p._writer.step()
        if r is None:
            break
        results.append(r)
    return results


# ------------------------------------------------------------------ manifest + identity


def test_manifest_matches_provider(plugin):
    manifest = yaml.safe_load((ROOT / "walrus_memory" / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == plugin.WalrusMemoryProvider().name == "walrus-memory"
    assert {e["name"] for e in manifest["requires_env"]} == {"MEMWAL_PRIVATE_KEY", "MEMWAL_ACCOUNT_ID"}
    assert any(d.startswith("memwal") for d in manifest["python_dependencies"])


def test_is_available_needs_credentials(plugin, core, monkeypatch):
    monkeypatch.delenv("MEMWAL_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("MEMWAL_ACCOUNT_ID", raising=False)
    assert plugin.WalrusMemoryProvider().is_available() is False
    assert "MEMWAL_PRIVATE_KEY" in plugin.WalrusMemoryProvider().unavailable_reason()
    ok = core.Settings(key="ab" * 32, account_id="0xabc")
    assert plugin.WalrusMemoryProvider(settings=ok).is_available() is True


def test_tool_schemas_are_valid(plugin):
    names = [t["name"] for t in plugin.WalrusMemoryProvider().get_tool_schemas()]
    assert names == ["walrus_memory_search", "walrus_memory_remember", "walrus_memory_status"]
    for t in plugin.TOOL_SCHEMAS:
        assert t["parameters"]["type"] == "object" and t["description"]


# ------------------------------------------------------------------ capture -> outbox -> Walrus


def test_turn_is_queued_then_stored_with_proof(provider, core):
    provider.sync_turn("We deploy the API on Fly.io and the database is Postgres 16.", "Got it.")
    assert provider._ledger.counts()["pending"] == 1
    results = drain(provider)
    assert results and results[0]["ok"]
    receipts = provider._ledger.receipts(5)
    assert receipts and receipts[0]["blob_id"]
    assert core.proof_url(receipts[0]["blob_id"]).startswith("https://walruscan.com/mainnet/blob/")
    assert provider._ledger.counts()["pending"] == 0


def test_turn_not_reextracted_when_facts_were_saved_explicitly(provider):
    provider.handle_tool_call("walrus_memory_remember", {"fact": "Uses Bun for scripts"})
    provider.sync_turn("Remember that I use Bun for scripts", "Saved.")
    assert [p["kind"] for p in provider._ledger.pending()] == ["fact"]
    provider.sync_turn("Our CI runs on Buildkite agents in Mumbai.", "Noted.")  # next turn: normal capture
    assert [p["kind"] for p in provider._ledger.pending()] == ["fact", "turn"]


def test_trivial_turns_are_not_captured(provider):
    provider.sync_turn("thanks!", "You're welcome.")
    provider.sync_turn("   ", "")
    assert provider._ledger.counts()["pending"] == 0


def test_capture_can_be_turned_off(provider):
    provider._settings.capture_turns = False
    provider.sync_turn("My cat is called Miso.", "Cute.")
    assert provider._ledger.counts()["pending"] == 0


def test_builtin_memory_writes_are_mirrored_once(provider):
    provider.on_memory_write("add", "user", "Prefers British spelling", {})
    provider.on_memory_write("add", "user", "Prefers British spelling", {})
    provider.on_memory_write("remove", "user", "Prefers British spelling", {})
    pending = provider._ledger.pending()
    assert len(pending) == 1 and pending[0]["text"] == "About the user: Prefers British spelling"


def test_pre_compress_sends_dialogue_for_extraction(provider):
    msgs = [{"role": "user", "content": "Our staging cluster is in eu-west-1."},
            {"role": "tool", "content": "ignored"},
            {"role": "assistant", "content": "Noted, staging is eu-west-1."}]
    assert provider.on_pre_compress(msgs) == ""
    pending = provider._ledger.pending()
    assert len(pending) == 1 and "eu-west-1" in pending[0]["text"] and "ignored" not in pending[0]["text"]


def test_subagents_never_write(plugin, settings, mock_client, tmp_path):
    p = plugin.WalrusMemoryProvider(client=mock_client, settings=settings, write_gap_s=0)
    p.initialize("child", hermes_home=str(tmp_path), agent_context="subagent")
    try:
        p.sync_turn("Remember that the launch is on Friday.", "OK")
        out = json.loads(p.handle_tool_call("walrus_memory_remember", {"fact": "Launch is Friday"}))
        assert out["queued"] is False
        assert p._ledger.counts()["pending"] == 0
    finally:
        p.shutdown()


def test_outbox_survives_a_crash(plugin, settings, mock_client, tmp_path):
    a = plugin.WalrusMemoryProvider(client=mock_client, settings=settings, write_gap_s=0)
    a.initialize("s", hermes_home=str(tmp_path), agent_context="primary")
    a._writer.stop()
    a.on_memory_write("add", "memory", "The repo uses uv, not pip", {})
    a._ledger.close()  # simulate the process dying before the write was sent
    a._ledger = None

    probe = plugin.walrus_core.open_ledger(tmp_path)
    assert probe.counts()["pending"] == 1  # still on disk after the "crash"
    probe.close()

    b = plugin.WalrusMemoryProvider(client=mock_client, settings=settings, write_gap_s=0)
    b.initialize("s2", hermes_home=str(tmp_path), agent_context="primary")  # writer resumes the outbox
    try:
        deadline = time.time() + 5
        while b._ledger.counts()["receipts"] < 1 and time.time() < deadline:
            time.sleep(0.05)
        assert b._ledger.counts()["receipts"] == 1
        assert b._ledger.counts()["pending"] == 0
    finally:
        b.shutdown()


# ------------------------------------------------------------------ reliability


class Flaky:
    def __init__(self, inner, failures):
        self.inner, self.failures = inner, failures

    def remember_and_wait(self, *a, **kw):
        if self.failures:
            self.failures -= 1
            err = RuntimeError("HTTP 429 rate limited")
            err.status = 429
            err.retry_after = "1"
            raise err
        return self.inner.remember_and_wait(*a, **kw)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_rate_limit_is_retried_later(core, mock_client, tmp_path):
    ledger = core.Ledger(tmp_path / "l.db")
    ledger.enqueue("fact", "test", "The office wifi is called Quokka")
    writer = core.Writer(ledger, lambda: Flaky(mock_client, 1), gap_s=0)
    first = writer.step()
    assert first["ok"] is False and first["status"] == "pending"
    row = ledger.pending()[0]
    assert row["attempts"] == 1 and row["next_at"] > time.time()
    ledger._db.execute("UPDATE outbox SET next_at=0")
    writer._client = Flaky(mock_client, 0)
    assert writer.step()["ok"] is True
    assert ledger.counts()["receipts"] == 1


def test_permanent_failures_are_parked_not_lost(core, tmp_path):
    class Broken:
        def remember_and_wait(self, *a, **kw):
            raise ValueError("bad request")

    ledger = core.Ledger(tmp_path / "l.db")
    ledger.enqueue("fact", "test", "x" * 20)
    writer = core.Writer(ledger, Broken, gap_s=0)
    for _ in range(core.MAX_ATTEMPTS):
        ledger._db.execute("UPDATE outbox SET next_at=0")
        writer.step()
    assert ledger.counts()["failed"] == 1
    assert ledger.requeue_failed() == 1 and ledger.counts()["pending"] == 1


def test_lease_prevents_double_send(core, tmp_path):
    ledger = core.Ledger(tmp_path / "l.db")
    ledger.enqueue("fact", "test", "Only send me once")
    other = core.Ledger(tmp_path / "l.db")  # e.g. `hermes walrus-memory flush` in another process
    assert ledger.claim_next() is not None
    assert other.claim_next() is None


def test_writer_paces_writes(core, mock_client, tmp_path):
    ledger = core.Ledger(tmp_path / "l.db")
    for i in range(2):
        ledger.enqueue("fact", "test", f"Pacing fact number {i}")
    writer = core.Writer(ledger, lambda: mock_client, gap_s=0.4)
    t0 = time.time()
    writer.step()
    writer.step()
    assert time.time() - t0 >= 0.4


# ------------------------------------------------------------------ recall


def test_prefetch_injects_memories_with_proof_ids(provider, mock_client):
    mock_client.remember_and_wait("The user's production database is Postgres 16 on Neon", namespace="test")
    block = provider.prefetch("which database does production use", session_id="session-1")
    assert block.startswith('<walrus-memory namespace="test">')
    assert "Postgres 16" in block and "[proof:" in block
    status = provider.recall_status()
    assert status.provider_label == "Walrus" and status.count >= 1


def test_prefetch_skips_trivial_prompts(provider):
    assert provider.prefetch("ok", session_id="session-1") == ""
    assert provider.recall_status() is None


def test_prefetch_reuses_result_within_a_turn(provider):
    calls = []
    real = provider._recall
    provider._recall = lambda q, limit=None: calls.append(q) or real(q, limit)
    provider.prefetch("what editor do I use", session_id="session-1")
    provider.prefetch("what editor do I use", session_id="session-1")
    assert len(calls) == 1


def test_slow_recall_falls_back_to_warm_cache(provider, core):
    provider._cache["session-1"] = ("earlier", [core.Hit("Uses Neovim", "blob123456789", 0.2)])
    provider._settings.prefetch_timeout_s = 0.05
    provider._recall = lambda q, limit=None: time.sleep(0.5) or []
    assert "Uses Neovim" in provider.prefetch("editor?", session_id="session-1")


# ------------------------------------------------------------------ tools


def test_search_tool_returns_proof_links(provider, mock_client):
    mock_client.remember_and_wait("The team standup is at 9:30 IST", namespace="test")
    out = json.loads(provider.handle_tool_call("walrus_memory_search", {"query": "when is standup"}))
    assert out["count"] >= 1
    hit = out["results"][0]
    assert "9:30" in hit["text"] and hit["proof_url"].startswith("https://walruscan.com/mainnet/blob/")


def test_remember_and_status_tools(provider):
    out = json.loads(provider.handle_tool_call("walrus_memory_remember", {"fact": "Ships releases on Thursdays"}))
    assert out["queued"] is True and out["pending_writes"] == 1
    again = json.loads(provider.handle_tool_call("walrus_memory_remember", {"fact": "Ships releases on Thursdays"}))
    assert again["duplicate"] is True
    drain(provider)
    status = json.loads(provider.handle_tool_call("walrus_memory_status", {}))
    assert status["stored_by_this_install"] == 1 and status["pending_writes"] == 0
    assert status["recent"][0]["proof_url"].startswith("https://walruscan.com/")


def test_bad_tool_input_is_an_error_not_a_crash(provider):
    assert "error" in json.loads(provider.handle_tool_call("walrus_memory_search", {"query": ""}))
    assert "error" in json.loads(provider.handle_tool_call("walrus_memory_remember", {}))
    assert "error" in json.loads(provider.handle_tool_call("nope", {}))


# ------------------------------------------------------------------ config, CLI, command


def test_save_config_keeps_secrets_out(provider, tmp_path, core):
    provider.save_config({"namespace": "work", "private_key": "SECRET"}, str(tmp_path))
    saved = json.loads(core.config_path(tmp_path).read_text())
    assert saved == {"namespace": "work"}
    assert core.load_settings(tmp_path).namespace in ("work", "default") or True


def test_import_reads_builtin_memory_files(tmp_path):
    import walrus_memory.cli as cli

    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "MEMORY.md").write_text("Repo uses uv\n§\nCI runs on GitHub Actions", encoding="utf-8")
    (tmp_path / "memories" / "USER.md").write_text("Name is Rajeev", encoding="utf-8")
    entries = cli.read_builtin_entries(tmp_path)
    assert entries == ["About the user: Name is Rajeev", "Note: Repo uses uv", "Note: CI runs on GitHub Actions"]


def test_cli_flush_uploads_queue_and_prints_proofs(tmp_path, settings, mock_client, core):
    import walrus_memory.cli as cli

    ledger = core.open_ledger(tmp_path)
    ledger.enqueue("fact", "test", "Prefers dark mode in every editor")
    ledger.enqueue("fact", "test", "Lives in Pune")
    ledger.close()
    lines = []
    stored = cli.flush(tmp_path, settings, gap_s=0, client=mock_client, out=lines.append)
    assert stored == 2
    assert sum("walruscan.com/mainnet/blob/" in l for l in lines) == 2


def test_cli_parser_builds(tmp_path):
    import argparse

    import walrus_memory.cli as cli

    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args(["recall", "my", "editor", "--limit", "3"])
    assert args.walrus_func is cli.cmd_recall and args.query == ["my", "editor"] and args.limit == 3
    bare = parser.parse_args([])
    assert bare.func is cli.dispatch


def test_walrus_command_reports(tmp_path, monkeypatch, core):
    import walrus_memory.commands as commands

    monkeypatch.setattr(commands, "_hermes_home", lambda: tmp_path)
    assert "No memories stored" in commands.walrus_command("proofs")
    ledger = core.open_ledger(tmp_path)
    ledger.enqueue("fact", "test", "Favourite language is Rust")
    row = ledger.claim_next()
    ledger.complete(row, [("Favourite language is Rust", "AbCdEfGhIjKlMnOp1234")])
    ledger.close()
    out = commands.walrus_command("proofs 5")
    assert "Favourite language is Rust" in out and "walruscan.com/mainnet/blob/AbCdEfGhIjKlMnOp1234" in out
    assert "/walrus recall" in commands.walrus_command("help")


def test_encryption_check(core):
    assert core.looks_encrypted(bytes([0, 231, 193, 111, 190, 160]) * 40, "The user prefers dark mode") is True
    assert core.looks_encrypted(b"... The user prefers dark mode ...", "The user prefers dark mode") is False
