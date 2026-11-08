"""Tusk driven through Hermes' real MemoryManager. Runs only inside a Hermes environment
(PYTHONPATH pointing at a hermes-agent checkout); skipped elsewhere."""

from __future__ import annotations

import json
import time

import pytest

memory_manager = pytest.importorskip("agent.memory_manager")


@pytest.fixture
def manager(plugin, settings, mock_client, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # the manager only serves the provider's own profile
    provider = plugin.TuskMemoryProvider(client=mock_client, settings=settings, write_gap_s=0)
    mgr = memory_manager.MemoryManager()
    mgr.add_provider(provider)
    mgr.initialize_all(session_id="it-1", platform="cli", hermes_home=str(tmp_path), agent_context="primary")
    yield mgr, provider
    mgr.shutdown_all()


def _wait(predicate, seconds=10.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_provider_is_a_real_memory_provider(plugin):
    from agent.memory_provider import MemoryProvider

    assert issubclass(plugin.TuskMemoryProvider, MemoryProvider)


def test_manager_routes_tools_to_tusk(manager, mock_client):
    # the SDK mock ranks by shared words, so queries reuse the stored wording
    mgr, _ = manager
    mock_client.remember_and_wait("The user deploys on Fly.io", namespace="test")
    out = json.loads(mgr.handle_tool_call("tusk_search", {"query": "where the user deploys"}))
    assert out["count"] >= 1 and "Fly.io" in out["results"][0]["text"]


def test_sync_through_manager_reaches_walrus(manager):
    mgr, provider = manager
    mgr.sync_all("Our staging database is Postgres 16 on Neon.", "Noted.")
    assert _wait(lambda: provider._ledger.counts()["receipts"] >= 1)


def test_prefetch_through_manager(manager, mock_client):
    mgr, _ = manager
    mock_client.remember_and_wait("The user's editor is Neovim with Gruvbox", namespace="test")
    block = mgr.prefetch_all("which editor is the user editor")
    assert "<tusk-memory" in block and "Neovim" in block


def test_tool_schemas_are_exposed(manager):
    mgr, _ = manager
    names = {s["name"] for s in mgr.get_all_tool_schemas()}
    assert {"tusk_search", "tusk_remember", "tusk_namespaces", "tusk_status"} <= names
