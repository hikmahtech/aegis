import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

export default function Integrations() {
  const [items, setItems] = useState<any[]>([]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [error, setError] = useState<Error | null>(null);
  const [msg, setMsg] = useState('');
  const [savingKey, setSavingKey] = useState('');

  async function load() {
    try { setItems(await api.getIntegrations()); } catch (e: any) { setError(e); }
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

      {groups.map(g => (
        <div key={g} className="card" style={{ marginBottom: 12 }}>
          <h3>{g}</h3>
          {items.filter(i => i.group === g).map(i => (
            <div key={i.key} className="cfg-row">
              <span className="cfg-label">
                {i.label} <span style={{ color: '#888', fontSize: 11 }}>({i.source})</span>
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
