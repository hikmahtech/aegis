import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

// Raw key/value settings editor. Moved off the Overview landing page so the
// first thing a user sees isn't a config table.
export default function Settings() {
  const [settings, setSettings] = useState<any[]>([]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setError(null); setLoading(true);
    try {
      setSettings(await api.listSettings());
    } catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function saveSetting(key: string) {
    try {
      const raw = edits[key];
      let value: any = raw;
      // Try to parse as JSON; if that fails, store as a plain string.
      try { value = JSON.parse(raw); } catch { /* keep as string */ }
      await api.updateSetting(key, value);
      setEdits(e => { const n = { ...e }; delete n[key]; return n; });
      await load();
    } catch (e: any) { setError(e); }
  }

  return (
    <div>
      <h1 className="page-title">Settings</h1>
      <p className="page-subtitle">Raw system settings. Values are parsed as JSON when possible, otherwise stored as text.</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {loading && <div className="loading">Loading settings…</div>}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ width: '20%' }}>Key</th>
              <th>Value</th>
              <th style={{ width: 180 }}>Updated</th>
              <th style={{ width: 80 }}></th>
            </tr>
          </thead>
          <tbody>
            {settings.map(s => {
              const raw = typeof s.value === 'string' ? s.value : JSON.stringify(s.value, null, 2);
              const current = edits[s.key] ?? raw;
              const dirty = edits[s.key] !== undefined && edits[s.key] !== raw;
              // Multi-line editor for anything long enough that a single input
              // would hide the body.
              const useTextarea = current.length > 60 || current.includes('\n');
              return (
                <tr key={s.key}>
                  <td><code style={{ wordBreak: 'break-all' }}>{s.key}</code></td>
                  <td>
                    {useTextarea ? (
                      <textarea
                        value={current}
                        onChange={e => setEdits(x => ({ ...x, [s.key]: e.target.value }))}
                        rows={Math.min(8, Math.max(2, current.split('\n').length))}
                        style={{ width: '100%', fontFamily: 'var(--mono)', fontSize: 12 }}
                      />
                    ) : (
                      <input
                        type="text"
                        value={current}
                        onChange={e => setEdits(x => ({ ...x, [s.key]: e.target.value }))}
                        style={{ width: '100%' }}
                      />
                    )}
                  </td>
                  <td className="meta">
                    {s.updated_at ? new Date(s.updated_at).toLocaleString() : '—'}
                  </td>
                  <td>{dirty && <button className="btn btn-sm btn-primary" onClick={() => void saveSetting(s.key)}>Save</button>}</td>
                </tr>
              );
            })}
            {!loading && settings.length === 0 && <tr><td colSpan={4} className="empty">No settings configured.</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
