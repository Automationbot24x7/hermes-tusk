"""The dashboard backend (tusk/dashboard/plugin_api.py), served through FastAPI's test client.
Runs where FastAPI is installed (any Hermes environment); skipped elsewhere."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(tmp_path, monkeypatch, mock_client):
    spec = importlib.util.spec_from_file_location("_tusk_dashboard_api_test", ROOT / "tusk/dashboard/plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "_home", lambda: tmp_path)
    monkeypatch.setattr(module.core, "make_client", lambda settings: mock_client)
    monkeypatch.setenv("MEMWAL_PRIVATE_KEY", "ab" * 32)
    monkeypatch.setenv("MEMWAL_ACCOUNT_ID", "0x" + "c" * 64)
    monkeypatch.setenv("MEMWAL_NAMESPACE", "test")
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/tusk")
    return module, TestClient(app), tmp_path


def _seed(module, home, text="Prefers dark mode", blob="BlobIdForTheDarkModeFact01"):
    ledger = module.outbox.open_ledger(module.core.data_dir(home))
    ledger.enqueue("fact", "test", text)
    ledger.complete(ledger.claim_next(), [(text, blob)])
    ledger.enqueue("fact", "test", "Still waiting to be sent")
    ledger.close()


def test_overview_reports_counts_queue_and_proofs(api):
    module, client, home = api
    _seed(module, home)
    body = client.get("/api/plugins/tusk/overview").json()
    assert body["configured"] is True and body["namespace"] == "test"
    assert body["counts"]["receipts"] == 1 and body["counts"]["pending"] == 1
    assert body["receipts"][0]["proof_url"].endswith("BlobIdForTheDarkModeFact01")
    assert body["pending"][0]["state"] == "queued"


def test_overview_when_not_configured(api, monkeypatch):
    _, client, _ = api
    monkeypatch.delenv("MEMWAL_PRIVATE_KEY")
    body = client.get("/api/plugins/tusk/overview").json()
    assert body["configured"] is False and body["signup_url"].startswith("https://")
    assert client.get("/api/plugins/tusk/recall", params={"q": "x"}).status_code == 409


def test_recall_and_namespaces(api, mock_client):
    _, client, _ = api
    mock_client.remember_and_wait("The user deploys the recipe app on Fly.io", namespace="test")
    found = client.get("/api/plugins/tusk/recall", params={"q": "user deploys recipe app"}).json()
    assert found["results"] and found["results"][0]["proof_url"].startswith("https://walruscan.com/")
    spaces = client.get("/api/plugins/tusk/namespaces").json()
    assert spaces["current"] == "test" and any(n["name"] == "test" for n in spaces["namespaces"])


def test_inputs_are_validated(api):
    _, client, _ = api
    assert client.get("/api/plugins/tusk/verify", params={"blob_id": "../../etc"}).status_code == 400
    assert client.get("/api/plugins/tusk/recall", params={"q": "x", "namespace": "a b"}).status_code == 400
    assert client.get("/api/plugins/tusk/recall", params={"q": "x", "limit": 99}).status_code == 422


def test_verify_compares_against_the_remembered_text(api, monkeypatch):
    module, client, home = api
    _seed(module, home)
    monkeypatch.setattr(
        module.core,
        "verify_blob",
        lambda blob_id, aggregator=None: {
            "ok": True,
            "status": 200,
            "bytes": 300,
            "body": bytes(range(256)) + b"x" * 44,
        },
    )
    body = client.get("/api/plugins/tusk/verify", params={"blob_id": "BlobIdForTheDarkModeFact01"}).json()
    assert body["ok"] and body["bytes"] == 300 and body["plaintext_absent"] is True
    assert body["hex"].startswith("00 01 02")
    other = client.get(
        "/api/plugins/tusk/verify", params={"blob_id": "SomeOtherBlobId0000001", "text": "xxxxx marker"}
    ).json()
    assert other["plaintext_absent"] is False  # the fake body contains "xxxxx"


def test_retry_requeues_failed_writes(api):
    module, client, home = api
    ledger = module.outbox.open_ledger(module.core.data_dir(home))
    ledger.enqueue("fact", "test", "Will fail")
    row = ledger.claim_next()
    for _ in range(module.outbox.MAX_ATTEMPTS):
        ledger.retry(row, "boom", 0)
        row = dict(row) | {"attempts": row["attempts"] + 1}
    ledger.close()
    assert client.post("/api/plugins/tusk/retry").json() == {"requeued": 1}
