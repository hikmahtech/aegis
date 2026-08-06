import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

// Email triage rules — the user-owned half of Gmail classification.
//
// The repo ships both lists empty so a fork carries nobody's senders; your
// rules live in the `email_triage_rules` settings row. Categories are a <select>
// rather than free text on purpose: the classifier's read path drops an invalid
// category rather than crashing, so a typed one would vanish silently.

type Sender = {
  email_addr: string;
  state: string;
  n: number;
  confidence: number;
  skips_llm: boolean;
};

const CATEGORY_HELP: Record<string, string> = {
  important_action: 'Interrupts you — creates a Todoist Inbox task, keeps the mail unread.',
  important_read: 'Labelled IMPORTANT and marked read. Findable, never interrupts.',
  informational: 'Marked read, IMPORTANT stripped. Out of the way.',
  useless: 'Same as informational — use it to record that a sender is pure noise.',
};

export default function EmailTriage() {
  const [categories, setCategories] = useState<string[]>([]);
  const [overrides, setOverrides] = useState<[string, string][]>([]);
  const [markers, setMarkers] = useState<string[]>([]);
  const [senders, setSenders] = useState<Sender[]>([]);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  async function load() {
    setError(null); setLoading(true);
    try {
      const r = await api.getEmailTriageRules();
      setCategories(r.categories || []);
      setOverrides(Object.entries(r.sender_overrides || {}) as [string, string][]);
      setMarkers(r.extra_notification_markers || []);
      setSenders(r.known_senders || []);
    } catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function save() {
    setError(null); setSaving(true); setSaved(false);
    try {
      const sender_overrides: Record<string, string> = {};
      for (const [addr, cat] of overrides) {
        if (addr.trim()) sender_overrides[addr.trim().toLowerCase()] = cat;
      }
      const r = await api.saveEmailTriageRules({
        sender_overrides,
        extra_notification_markers: markers.map(m => m.trim()).filter(Boolean),
      });
      setOverrides(Object.entries(r.sender_overrides || {}) as [string, string][]);
      setMarkers(r.extra_notification_markers || []);
      setSenders(r.known_senders || []);
      setSaved(true);
    } catch (e: any) { setError(e); }
    finally { setSaving(false); }
  }

  const ruled = useMemo(
    () => new Set(overrides.map(([a]) => a.trim().toLowerCase())),
    [overrides],
  );
  // Senders that short-circuit the LLM and aren't covered by a rule. A wrong
  // verdict here cannot self-correct: a cache hit never reaches the LLM, and
  // only the LLM path re-teaches the cache. These are what overrides are for.
  const stuck = senders.filter(s => s.skips_llm && !ruled.has(s.email_addr));

  return (
    <div>
      <h1 className="page-title">Email triage</h1>
      <p className="page-subtitle">
        Your senders and phrases, kept out of the codebase. AEGIS ships these empty.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {loading && <div className="loading">Loading rules…</div>}

      {stuck.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <h2 className="section-title">Stuck senders ({stuck.length})</h2>
          <p className="meta" style={{ marginBottom: 8 }}>
            These have enough history (n≥3, confidence≥0.75) that the classifier trusts
            the cache and never calls the LLM for them — so if the cached verdict is
            wrong it <strong>cannot fix itself</strong>. An override is the only thing that
            changes them. Click one to add a rule.
          </p>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr><th>Sender</th><th style={{ width: 150 }}>Cached as</th>
                  <th style={{ width: 60 }}>n</th><th style={{ width: 80 }}>conf</th>
                  <th style={{ width: 90 }} /></tr>
              </thead>
              <tbody>
                {stuck.slice(0, 20).map(s => (
                  <tr key={s.email_addr}>
                    <td><code>{s.email_addr}</code></td>
                    <td>{s.state}</td>
                    <td>{s.n}</td>
                    <td>{s.confidence}</td>
                    <td>
                      <button
                        className="btn btn-sm"
                        onClick={() => setOverrides(o => [...o, [s.email_addr, 'informational']])}
                      >+ Rule</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Sender rules</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          Checked <strong>first</strong> — ahead of the reputation cache and the LLM — so a rule
          decides outright and costs no LLM call. Use a bare domain (<code>@substack.com</code>)
          to cover everyone at it; an exact address beats its domain rule.
          Deleting a rule genuinely stops it applying: rules never write learned state.
        </p>
        <p className="meta" style={{ marginBottom: 12 }}>
          ⚠️ A rule returns no content tags, and the receipt/money fan-out keys on those
          tags — so <strong>don't override banks, payment or receipt senders</strong> or you
          silently disable receipt extraction for them.
        </p>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr><th>Sender or @domain</th><th style={{ width: 220 }}>Treat as</th>
                <th style={{ width: 320 }}>What that does</th><th style={{ width: 50 }} /></tr>
            </thead>
            <tbody>
              {overrides.map(([addr, cat], i) => (
                <tr key={i}>
                  <td>
                    <input
                      type="text" value={addr} list="known-senders"
                      placeholder="name@example.com or @example.com"
                      onChange={e => setOverrides(o => o.map((r, j) => j === i ? [e.target.value, r[1]] : r))}
                      style={{ width: '100%' }}
                    />
                  </td>
                  <td>
                    <select
                      value={cat}
                      onChange={e => setOverrides(o => o.map((r, j) => j === i ? [r[0], e.target.value] : r))}
                    >
                      {categories.map(c => <option key={c} value={c}>{c}</option>)}
                    </select>
                  </td>
                  <td className="meta">{CATEGORY_HELP[cat] || ''}</td>
                  <td>
                    <button className="btn btn-sm" onClick={() => setOverrides(o => o.filter((_, j) => j !== i))}>✕</button>
                  </td>
                </tr>
              ))}
              {overrides.length === 0 && (
                <tr><td colSpan={4} className="empty">No sender rules. The classifier decides everything.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <datalist id="known-senders">
          {senders.map(s => <option key={s.email_addr} value={s.email_addr} />)}
        </datalist>
        <button
          className="btn" style={{ marginTop: 8 }}
          onClick={() => setOverrides(o => [...o, ['', 'informational']])}
        >+ Add sender rule</button>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Notification phrases</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          Subject substrings (matched case-insensitively) that mean “this is a courtesy
          notice”. A match can only ever <em>demote</em> a message out of{' '}
          <code>important_action</code> — it never hides mail. Use it for phrasing
          specific to your bank or tooling, e.g. <code>incorrect login attempt</code>.
          Generic ones (sign-in alerts, OTPs, connection requests) are already built in.
        </p>
        {markers.map((m, i) => (
          <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 6 }}>
            <input
              type="text" value={m} placeholder="incorrect login attempt"
              onChange={e => setMarkers(x => x.map((v, j) => j === i ? e.target.value : v))}
              style={{ flex: 1 }}
            />
            <button className="btn btn-sm" onClick={() => setMarkers(x => x.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
        {markers.length === 0 && <div className="empty">No extra phrases.</div>}
        <button className="btn" style={{ marginTop: 8 }} onClick={() => setMarkers(m => [...m, ''])}>
          + Add phrase
        </button>
      </div>

      <div style={{ marginTop: 16 }}>
        <button className="btn btn-primary" disabled={saving} onClick={save}>
          {saving ? 'Saving…' : 'Save rules'}
        </button>
        {saved && <span className="meta" style={{ marginLeft: 12 }}>Saved. Applies from the next hourly run.</span>}
      </div>
    </div>
  );
}
