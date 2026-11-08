# Tusk: Hermes memory on Walrus

**Hermes' long-term memory, stored on Walrus mainnet.** Encrypted, owned by your key, still there after you wipe or reinstall Hermes, the same on every machine (and for every agent that uses the same Walrus Memory account). Each memory comes with a public proof link.

> Tusk is a community plugin built on the official Walrus Memory SDK. It is not affiliated with Mysten Labs or the Walrus Foundation.

```text
you ──► Hermes ──► tusk ──► Walrus Memory relayer (TEE) ──► Walrus blobs on Sui mainnet
          ▲              │  durable outbox (SQLite), paced writes,        │
          └── recall ◄───┘  receipts with blob ids  ◄────────────────────┘
```

| Hermes surface | What this plugin does with it |
|---|---|
| **Memory provider** | Recalls relevant memories before every turn, distills finished turns into facts, mirrors the built-in `MEMORY.md` / `USER.md` writes, and saves insights before context compression |
| **Tools** | `tusk_search` (results carry proof links), `tusk_remember`, `tusk_status` |
| **Slash command** | `/tusk` shows status, proofs, recall, verify and retry from inside any session |
| **CLI** | `hermes tusk status / recall / proofs / verify / import / flush / restore / retry` |

## Why another memory provider?

Every other memory backend keeps your agent's memory in one vendor's database or one machine's disk. This one doesn't:

- **You own it.** Memories are encrypted by the Walrus Memory relayer inside a TEE, sealed with SEAL and stored as Walrus blobs owned by your account. The plugin only signs requests with your delegate key.
- **It follows you.** Wipe `~/.hermes`, reinstall, or start Hermes on another computer: add the same key and Hermes already knows you. Other agents on the same account and namespace (Walrus Memory has official plugins for other agent tools) read and write the same memory.
- **It's verifiable.** Every stored memory has a blob id. `/tusk proofs` lists Walruscan links, and `hermes tusk verify` downloads the blobs straight from a public Walrus aggregator, with no relayer and no key, and checks they are ciphertext.
- **It doesn't lose writes.** Writes go to a local SQLite outbox first and a background thread sends them, paced under the relayer's rate limit, with retries and backoff. If Hermes crashes or you close the terminal, the queue resumes next time.
- **It can be rebuilt.** `hermes tusk restore` asks the relayer to rebuild a namespace's search index from the blobs on Walrus.

## Quick start

1. **Get credentials.** Sign in at [memory.walrus.xyz](https://memory.walrus.xyz), create a delegate key, and copy the delegate **private key** and your **account id**.
2. **Install**
   ```bash
   hermes plugins install tusk          # from the Hermes plugin catalog
   # or straight from GitHub:
   hermes plugins install Automationbot24x7/hermes-tusk/tusk
   ```
3. **Activate**
   ```bash
   hermes memory setup        # pick tusk, paste the key and account id
   ```
   or put these in your profile's `.env` and set the provider:
   ```bash
   MEMWAL_PRIVATE_KEY=...        # delegate private key (hex)
   MEMWAL_ACCOUNT_ID=0x...       # account id
   MEMWAL_NAMESPACE=default      # optional: share a namespace with your other agents
   ```
   ```bash
   hermes config set memory.provider tusk
   ```
4. **Check it:** `hermes tusk status`

Already have memories in Hermes? `hermes tusk import` copies your built-in `MEMORY.md` and `USER.md` entries to Walrus.

## Using it

Just talk to Hermes. Before each turn the plugin recalls what's relevant and injects it as a `<tusk-memory>` block, each line tagged with a short proof id. After each turn it queues the exchange for fact extraction. Anything Hermes saves with its built-in memory tool is mirrored to Walrus too.

```text
/tusk                       status, latest proofs
/tusk proofs 10             last 10 memories with Walruscan links
/tusk recall editor         semantic search across all your machines and agents
/tusk verify 3              fetch the last 3 blobs directly from Walrus, check they are encrypted
/tusk retry                 re-queue writes that failed
```

```text
hermes tusk status          account, namespace, relayer health, queue, proofs
hermes tusk recall <query>  semantic search with relevance and proof links
hermes tusk proofs          latest stored memories
hermes tusk verify          check blobs on a public aggregator
hermes tusk import          copy built-in MEMORY.md / USER.md entries to Walrus
hermes tusk flush           upload everything still queued, now
hermes tusk restore         rebuild the namespace index from Walrus blobs
hermes tusk retry           re-queue failed writes
```

## Configuration

Secrets come only from Hermes (`hermes memory setup`, the profile `.env`, or a configured secret source), read through Hermes' profile-aware secret scope. Optional settings go in `$HERMES_HOME/tusk.json`:

| Key | Default | Meaning |
|---|---|---|
| `namespace` | `default` | Memory namespace. Use the same one as your other agents to share memory (`MEMWAL_NAMESPACE` overrides) |
| `server_url` | `https://relayer.memory.walrus.xyz` | Relayer (`MEMWAL_SERVER_URL` overrides) |
| `capture_turns` | `true` | Distill every finished turn into facts. Set `false` to store only explicit and mirrored memories |
| `recall_limit` | `6` | Memories injected per turn |
| `max_distance` | `0.8` | Recall cutoff (lower is stricter) |
| `prefetch_timeout_s` | `6` | Longest a turn waits for live recall before using the last background recall |

## Reliability details

- **Durable outbox.** `plugin-data/tusk/ledger.db` (SQLite, WAL). Identical content is queued once, and each write carries an idempotency key, so a retry after a crash can't store a duplicate.
- **Pacing and retries.** About one write every 11 s, under the relayer's per-key limit. A 429 response waits for `Retry-After`. Other errors back off exponentially, and after 6 attempts a write is parked as failed (never dropped). `/tusk retry` re-queues it.
- **No double sends.** Each job is leased atomically, so a running Hermes and `hermes tusk flush` never send the same write twice.
- **Never blocks a turn.** `sync_turn` is one SQLite insert. Recall is capped at `prefetch_timeout_s`, after which the turn uses the background recall warmed at the end of the previous turn.
- **Right agent, right profile.** Subagents, cron runs and flush forks never write. All state is bound to the profile's `hermes_home`, and background threads use `spawn_context_thread`.
- **No duplicate facts.** If a fact was saved explicitly during a turn, that turn isn't re-extracted.

## Disclosure

- **Network:** the plugin calls `relayer.memory.walrus.xyz` (or your `MEMWAL_SERVER_URL`). It sends each turn's user message as the recall query, each finished turn (user and assistant text, up to about 2,400 characters each) for fact extraction, built-in memory entries, and anything passed to `tusk_remember`. `verify` additionally downloads blobs from `aggregator.walrus-mainnet.walrus.space`. Walruscan links are only printed, never fetched.
- **Local data:** queued writes (plaintext until sent) and receipts (memory text plus blob id) are kept in `$HERMES_HOME/plugin-data/tusk/ledger.db`.
- **Credentials:** read only through Hermes' secret scope. The plugin never reads credential files and never writes your key anywhere.
- **Processes:** one in-process background thread drains the outbox. No shell commands, no subprocesses, no telemetry.
- **Cost:** storage and extraction count against your Walrus Memory account.

## Known limits

- The relayer API has no per-memory delete yet (only whole-namespace erasure), so removing an entry from Hermes' built-in memory does not delete its Walrus copy.
- Writes take roughly 10 to 20 s each and are paced. One-shot runs (`hermes chat -q`) usually exit before the queue drains. The writes stay queued and go out on the next run or with `hermes tusk flush`.

## Development

```bash
pip install "memwal>=0.2.2,<0.3" pytest pyyaml
pytest                      # 28 offline tests using the SDK's in-process mock
hermes plugins validate ./tusk --install-deps
```

The plugin lives in [`tusk/`](tusk). Tests are in [`tests/`](tests).

## License

MIT
