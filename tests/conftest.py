"""Load the plugin as a package and, outside Hermes, provide a minimal agent.memory_provider."""

from __future__ import annotations

import importlib.util
import re
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "tusk"


def _install_agent_stub() -> None:
    try:
        import agent.memory_provider  # noqa: F401  (running inside a Hermes checkout)

        return
    except ImportError:
        pass

    mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # the real one is an ABC with the same method names
        pass

    @dataclass
    class RecallStatus:
        provider_label: str
        count: int
        glyph: str = "*"

    trivial = re.compile(r"^(yes|no|ok|okay|sure|thanks|thank you|hi|hey|hello|done|next)[\s!?.]*$", re.I)

    def is_trivial_prompt(text):
        return not (text or "").strip() or bool(trivial.match(text.strip()))

    def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):
        return threading.Thread(target=target, name=name, daemon=daemon, args=args, kwargs=kwargs)

    mod.MemoryProvider = MemoryProvider
    mod.RecallStatus = RecallStatus
    mod.is_trivial_prompt = is_trivial_prompt
    mod.spawn_context_thread = spawn_context_thread
    pkg = types.ModuleType("agent")
    pkg.__path__ = []
    pkg.memory_provider = mod
    sys.modules.setdefault("agent", pkg)
    sys.modules["agent.memory_provider"] = mod


def _load_plugin():
    if "tusk" in sys.modules:
        return sys.modules["tusk"]
    spec = importlib.util.spec_from_file_location(
        "tusk", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["tusk"] = module
    spec.loader.exec_module(module)
    return module


_install_agent_stub()
PLUGIN = _load_plugin()


@pytest.fixture
def plugin():
    return PLUGIN


@pytest.fixture
def core():
    from tusk import core

    return core


@pytest.fixture
def outbox():
    from tusk import outbox

    return outbox


@pytest.fixture
def settings(core):
    return core.Settings(key="ab" * 32, account_id="0x" + "c" * 64, namespace="test", prefetch_timeout_s=5.0)


@pytest.fixture
def mock_client():
    from memwal import MemWalMockSync

    return MemWalMockSync.create(namespace="test")


@pytest.fixture
def provider(plugin, settings, mock_client, tmp_path):
    p = plugin.TuskMemoryProvider(client=mock_client, settings=settings, write_gap_s=0)
    p.initialize("session-1", hermes_home=str(tmp_path), platform="cli", agent_context="primary")
    p._writer.stop()  # tests drive the writer step by step
    yield p
    p.shutdown()
