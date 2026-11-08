// Tusk dashboard tab: browse, search and verify Hermes' long-term memory on Walrus.
// Plain IIFE against the dashboard plugin SDK (React comes from the SDK; no build step).
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Button, Input } = SDK.components;
  const h = React.createElement;

  const API = "/api/plugins/tusk";
  const getJSON = (path) => SDK.fetchJSON(API + path);
  const postJSON = (path) => SDK.fetchJSON(API + path, { method: "POST" });
  const ago = (ts) => (SDK.utils.timeAgo ? SDK.utils.timeAgo(ts) : new Date(ts * 1000).toLocaleString());
  const shortId = (id) => (id && id.length > 14 ? id.slice(0, 8) + "…" + id.slice(-4) : id || "");
  const REFRESH_MS = 4000;
  const SPACES_REFRESH_MS = 30000;

  function ProofLink({ url, blobId }) {
    return h("a", { className: "tusk-proof", href: url, target: "_blank", rel: "noopener noreferrer",
                    title: "Open this blob on Walruscan" }, shortId(blobId), " ↗");
  }

  function VerifyResult({ result }) {
    if (result === "loading") {
      return h("div", { className: "tusk-verify tusk-verify--busy" }, "Downloading the blob from a public Walrus aggregator…");
    }
    if (!result) return null;
    if (result.error) return h("div", { className: "tusk-verify tusk-verify--bad" }, result.error);
    if (!result.ok) {
      return h("div", { className: "tusk-verify tusk-verify--bad" }, `Not retrievable from Walrus (HTTP ${result.status}).`);
    }
    const sealed = result.plaintext_absent;
    return h("div", { className: "tusk-verify" },
      h("div", { className: "tusk-verify__line" },
        h("span", { className: "tusk-ok" }, "✓ on Walrus mainnet"),
        ` ${result.bytes} bytes fetched in ${(result.took_ms / 1000).toFixed(1)} s from ${result.aggregator.replace("https://", "")}, no relayer, no key`),
      sealed === undefined || sealed === null ? null : h("div", { className: "tusk-verify__line" },
        sealed ? h("span", { className: "tusk-ok" }, "✓ encrypted")
               : h("span", { className: "tusk-bad" }, "✗ plaintext visible"),
        sealed ? " none of the remembered words appear in the stored bytes" : ""),
      h("code", { className: "tusk-hex" }, result.hex + " …"));
  }

  function MemoryRow({ text, meta, blobId, proofUrl, relevance, verify, onVerify }) {
    return h("li", { className: "tusk-row" },
      h("div", { className: "tusk-row__main" },
        h("div", { className: "tusk-row__text" }, text),
        h("div", { className: "tusk-row__meta" },
          relevance === undefined ? null : h("span", { className: "tusk-rel" },
            h("span", { className: "tusk-rel__bar", style: { width: `${Math.round(relevance * 100)}%` } })),
          relevance === undefined ? null : h("span", null, `${Math.round(relevance * 100)}% match`),
          meta ? h("span", null, meta) : null,
          blobId ? h(ProofLink, { url: proofUrl, blobId }) : null),
        h(VerifyResult, { result: verify })),
      blobId ? h(Button, { size: "sm", outlined: true, onClick: () => onVerify(blobId, text),
                          disabled: verify === "loading" }, "Verify") : null);
  }

  function Stat({ label, value, hint }) {
    return h("div", { className: "tusk-stat" },
      h("div", { className: "tusk-stat__value" }, value === null || value === undefined ? "…" : String(value)),
      h("div", { className: "tusk-stat__label" }, label),
      hint ? h("div", { className: "tusk-stat__hint" }, hint) : null);
  }

  function NotConfigured({ signupUrl }) {
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Connect Tusk to Walrus")),
      h(CardContent, null,
        h("ol", { className: "tusk-steps" },
          h("li", null, "Create a delegate key at ", h("a", { href: signupUrl, target: "_blank", rel: "noopener noreferrer" }, signupUrl), "."),
          h("li", null, "Run ", h("code", null, "hermes memory setup"), " and pick tusk, or set MEMWAL_PRIVATE_KEY and MEMWAL_ACCOUNT_ID in Keys."),
          h("li", null, "Come back here: your memories, proofs and queue appear live."))));
  }

  function TuskPage() {
    const [overview, setOverview] = useState(null);
    const [spaces, setSpaces] = useState(null);
    const [spaceError, setSpaceError] = useState("");
    const [query, setQuery] = useState("");
    const [searchSpace, setSearchSpace] = useState("");
    const [results, setResults] = useState(null);
    const [searching, setSearching] = useState(false);
    const [searchError, setSearchError] = useState("");
    const [verified, setVerified] = useState({});

    const refresh = useCallback(() => getJSON("/overview").then(setOverview).catch(() => {}), []);

    useEffect(() => {
      let timer = null;
      let stopped = false;
      const tick = () => {
        const next = () => { if (!stopped) timer = setTimeout(tick, REFRESH_MS); };
        if (document.hidden) return next();  // no polling while the tab is in the background
        refresh().finally(next);
      };
      tick();
      return () => { stopped = true; clearTimeout(timer); };
    }, [refresh]);

    useEffect(() => {
      let timer = null;
      const load = () => getJSON("/namespaces")
        .then((r) => { setSpaces(r); setSpaceError(""); timer = setTimeout(load, SPACES_REFRESH_MS); })
        .catch(() => { setSpaceError("Relayer unreachable, retrying…"); timer = setTimeout(load, 8000); });
      load();
      return () => clearTimeout(timer);
    }, []);

    const verify = useCallback((blobId, text) => {
      setVerified((v) => ({ ...v, [blobId]: "loading" }));
      const compare = text ? `&text=${encodeURIComponent(text.slice(0, 1000))}` : "";
      getJSON(`/verify?blob_id=${encodeURIComponent(blobId)}${compare}`)
        .then((r) => setVerified((v) => ({ ...v, [blobId]: r })))
        .catch((e) => setVerified((v) => ({ ...v, [blobId]: { error: String(e.message || e) } })));
    }, []);

    const search = useCallback((event) => {
      if (event) event.preventDefault();
      if (!query.trim()) return;
      setSearching(true);
      setSearchError("");
      const ns = searchSpace ? `&namespace=${encodeURIComponent(searchSpace)}` : "";
      getJSON(`/recall?q=${encodeURIComponent(query.trim())}${ns}`)
        .then((r) => setResults(r))
        .catch((e) => setSearchError(String(e.message || e)))
        .finally(() => setSearching(false));
    }, [query, searchSpace]);

    if (!overview) return h("div", { className: "tusk-page" }, h("p", { className: "tusk-muted" }, "Loading Tusk…"));
    if (!overview.configured) return h("div", { className: "tusk-page" }, h(NotConfigured, { signupUrl: overview.signup_url }));

    const c = overview.counts || {};
    const current = spaces && spaces.namespaces.find((n) => n.name === overview.namespace);
    const inFlight = c.in_flight ? `${c.in_flight} sending now` : "";

    return h("div", { className: "tusk-page" },
      h("header", { className: "tusk-hero" },
        h("p", { className: "tusk-tagline" }, "Hermes' long-term memory on Walrus mainnet: encrypted, owned by your key, the same on every machine and agent."),
        h("div", { className: "tusk-chips" },
          h("span", { className: "tusk-chip" }, h("span", { className: "tusk-chip__k" }, "namespace"), overview.namespace),
          h("span", { className: "tusk-chip" }, h("span", { className: "tusk-chip__k" }, "account"), overview.account))),

      h("section", { className: "tusk-stats" },
        h(Stat, { label: "memories on Walrus", value: current ? current.memories : (spaces ? 0 : null),
                  hint: `in '${overview.namespace}', all machines` }),
        h(Stat, { label: "stored by this install", value: c.receipts }),
        h(Stat, { label: "queued", value: c.pending, hint: inFlight }),
        h(Stat, { label: "failed", value: c.failed, hint: c.failed ? "retry below" : "" })),

      h(Card, null,
        h(CardHeader, null, h(CardTitle, null, "Search your memory")),
        h(CardContent, null,
          h("form", { className: "tusk-search", onSubmit: search },
            h(Input, { value: query, placeholder: "What do I know about… (semantic, across machines and agents)",
                       onChange: (e) => setQuery(e.target.value) }),
            h("select", { className: "tusk-select", value: searchSpace, onChange: (e) => setSearchSpace(e.target.value),
                          "aria-label": "Namespace" },
              h("option", { value: "" }, overview.namespace),
              (spaces ? spaces.namespaces : []).filter((n) => n.name !== overview.namespace)
                .map((n) => h("option", { key: n.name, value: n.name }, `${n.name} (${n.memories})`))),
            h(Button, { type: "submit", disabled: searching || !query.trim() }, searching ? "Searching…" : "Search")),
          searchError ? h("p", { className: "tusk-bad" }, searchError) : null,
          results ? (results.results.length
            ? h("ul", { className: "tusk-list" }, results.results.map((r) => h(MemoryRow, {
                key: r.blob_id || r.text, text: r.text, blobId: r.blob_id, proofUrl: r.proof_url,
                relevance: r.relevance, verify: verified[r.blob_id], onVerify: verify })))
            : h("p", { className: "tusk-muted" }, `Nothing in '${results.namespace}' matches.`)) : null)),

      h("div", { className: "tusk-grid" },
        h(Card, null,
          h(CardHeader, null, h(CardTitle, null, "Latest proofs")),
          h(CardContent, null,
            overview.receipts.length
              ? h("ul", { className: "tusk-list" }, overview.receipts.map((r) => h(MemoryRow, {
                  key: r.blob_id + r.stored_at, text: r.text, blobId: r.blob_id, proofUrl: r.proof_url,
                  meta: ago(r.stored_at) + (r.namespace !== overview.namespace ? ` · ${r.namespace}` : ""),
                  verify: verified[r.blob_id], onVerify: verify })))
              : h("p", { className: "tusk-muted" }, "Nothing stored from this install yet. Memories written elsewhere still show up in search."))),

        h("div", { className: "tusk-side" },
          h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Write queue")),
            h(CardContent, null,
              !overview.pending.length && !overview.failed.length
                ? h("p", { className: "tusk-muted" }, "Empty: everything is on Walrus. Writes are paced under the relayer's rate limit and survive restarts.")
                : null,
              overview.pending.length ? h("ul", { className: "tusk-queue" }, overview.pending.map((p, i) => h("li", { key: i },
                h("span", { className: `tusk-state tusk-state--${p.state}` },
                  p.state === "retrying" ? `retry in ${p.retry_in_s}s` : p.state),
                h("span", { className: "tusk-queue__text" }, `${p.kind === "turn" ? "turn → facts" : "fact"}: ${p.text}`)))) : null,
              overview.failed.length ? h("div", { className: "tusk-failed" },
                h("ul", { className: "tusk-queue" }, overview.failed.map((f, i) => h("li", { key: i },
                  h("span", { className: "tusk-state tusk-state--failed" }, "failed"),
                  h("span", { className: "tusk-queue__text" }, f.error)))),
                h(Button, { size: "sm", outlined: true, onClick: () => postJSON("/retry").then(refresh) }, "Retry failed writes")) : null)),

          h(Card, null,
            h(CardHeader, null, h(CardTitle, null, "Namespaces")),
            h(CardContent, null,
              spaceError ? h("p", { className: "tusk-bad" }, spaceError) : null,
              spaces ? h("ul", { className: "tusk-spaces" }, spaces.namespaces.map((n) => h("li", { key: n.name },
                h("button", { type: "button", className: "tusk-space", onClick: () => setSearchSpace(n.name === overview.namespace ? "" : n.name),
                              title: "Search this namespace" },
                  h("span", null, n.name === overview.namespace ? `${n.name} (Hermes)` : n.name),
                  h("span", { className: "tusk-muted" }, n.memories))))) : h("p", { className: "tusk-muted" }, "Loading…"),
              h("p", { className: "tusk-hint" }, "Other agents on this account write to their own namespaces. Pick one to search it."))))));
  }

  window.__HERMES_PLUGINS__.register("tusk", TuskPage);
})();
