"""Live round trip on Walrus mainnet. Opt in with TUSK_LIVE=1 plus MEMWAL_PRIVATE_KEY and
MEMWAL_ACCOUNT_ID; writes one memory to the `tusk-live-test` namespace."""

from __future__ import annotations

import os
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("TUSK_LIVE") == "1"
        and os.environ.get("MEMWAL_PRIVATE_KEY")
        and os.environ.get("MEMWAL_ACCOUNT_ID")
    ),
    reason="live test: set TUSK_LIVE=1 and Walrus Memory credentials",
)


def test_remember_recall_and_verify_on_mainnet(core, outbox, tmp_path):
    settings = core.Settings(
        key=os.environ["MEMWAL_PRIVATE_KEY"], account_id=os.environ["MEMWAL_ACCOUNT_ID"], namespace="tusk-live-test"
    )
    marker = uuid.uuid4().hex[:8]
    fact = f"Tusk live check {marker}: the user's test parrot is called Biscuit"

    ledger = outbox.open_ledger(tmp_path)
    ledger.enqueue("fact", settings.namespace, fact)
    client = core.make_client(settings)
    try:
        result = outbox.Writer(ledger, lambda: client, gap_s=0).step()
        assert result and result["ok"], result
        blob_id = result["receipts"][0][1]

        hits = []
        for _ in range(6):  # the index can lag the write by a few seconds
            hits = core.recall(client, f"parrot Biscuit {marker}", settings, limit=5)
            if any(marker in h.text for h in hits):
                break
            time.sleep(3)
        assert any(marker in h.text and h.blob_id == blob_id for h in hits)
    finally:
        client.close()
        ledger.close()

    blob = core.verify_blob(blob_id)
    assert blob["ok"] and blob["bytes"] > 0
    assert core.plaintext_absent(blob["body"], fact), "the blob on Walrus must be ciphertext"
