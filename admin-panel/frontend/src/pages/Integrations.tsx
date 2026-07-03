import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

export default function Integrations() {
  const [items, setItems] = useState<any[]>([]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [error, setError] = useState<Error | null>(null);
  const [msg, setMsg] = useState('');
  const [savingKey, setSavingKey] = useState('');
  const [keyStatus, setKeyStatus] = useState<{ configured: boolean; source: string } | null>(null);
  const [newKey, setNewKey] = useState('');
  const [generating, setGenerating] = useState(false);
  const [copied, setCopied] = useState(false);

  async function load() {
    try { setItems(await api.getIntegrations()); } catch (e: any) { setError(e); }
    try { setKeyStatus(await api.getApiKeyStatus()); } catch { /* status is best-effort */ }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function save(key: string) {
    setSavingKey(key); setMsg(''); setError(null);
    try {
      setItems(await api.saveIntegration(key, edits[key] ?? ''));
      setEdits(e => { const n = { ...e }; delete n[key]; return n; });
      setMsg(`Saved ${key}.`);
    } catch (e: any) { setError(e); } finally { setSavingKey(''); }
  }

  async function generateKey() {
    if (keyStatus?.source === 'db' &&
        !window.confirm('Generate a new API key? The previously generated key stops working immediately.')) {
      return;
    }
    setGenerating(true); setError(null); setCopied(false);
    try {
      const res = await api.generateApiKey();
      setNewKey(res.api_key);
      setKeyStatus({ configured: true, source: 'db' });
    } catch (e: any) { setError(e); } finally { setGenerating(false); }
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(newKey);
      setCopied(true);
    } catch { /* clipboard unavailable — user can select the text */ }
  }

  const groups = Array.from(new Set(items.map(i => i.group)));

  return (
    <div>
      <h1 className="page-title">Integrations &amp; Secrets</h1>
      <p className="page-subtitle">
        Connector tokens + webhook secrets, stored encrypted in the DB (your env vars are the fallback).
        Token changes apply on the next worker restart; webhook secrets go live on save.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {msg && <p className="msg-success">{msg}</p>}

      <div className="card" style={{ marginBottom: 12 }}>
        <h3>API Key</h3>
        <p style={{ color: 'var(--text-muted)', fontSize: 13, margin: '4px 0 10px' }}>
          Key for programmatic access to the AEGIS API (sent as <code>X-API-Key</code>).
          Generated server-side and stored encrypted; applies within seconds, no restart needed.
          {keyStatus && (
            <> Currently: <strong>{keyStatus.configured ? `configured (${keyStatus.source})` : 'not configured'}</strong>.</>
          )}
        </p>
        <button className="btn" disabled={generating} onClick={() => void generateKey()}>
          {generating ? 'Generating…' : 'Generate API key'}
        </button>
        {newKey && (
          <div style={{ marginTop: 10 }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <code data-testid="new-api-key" style={{ wordBreak: 'break-all', padding: '6px 8px', background: 'var(--bg-inset, rgba(128,128,128,0.12))', borderRadius: 4 }}>
                {newKey}
              </code>
              <button className="btn btn-sm" onClick={() => void copyKey()}>{copied ? 'Copied ✓' : 'Copy'}</button>
            </div>
            <p style={{ color: 'var(--warning, #b58900)', fontSize: 12, marginTop: 6 }}>
              Copy it now — this key is shown only once and cannot be retrieved again.
              Generating a new key replaces this one.
            </p>
          </div>
        )}
      </div>

      {groups.map(g => (
        <div key={g} className="card" style={{ marginBottom: 12 }}>
          <h3>{g}</h3>
          {items.filter(i => i.group === g).map(i => (
            <div key={i.key} className="cfg-row">
              <span className="cfg-label">
                {i.label} <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>({i.source})</span>
              </span>
              <input
                type={i.secret ? 'password' : 'text'}
                value={i.secret ? (edits[i.key] ?? '') : (edits[i.key] ?? i.value ?? '')}
                onChange={e => setEdits(ed => ({ ...ed, [i.key]: e.target.value }))}
                placeholder={i.secret ? (i.set ? '•••••••• (set — enter to replace)' : 'not set') : ''}
              />
              <button className="btn" disabled={savingKey === i.key || edits[i.key] === undefined}
                onClick={() => save(i.key)}>Save</button>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
