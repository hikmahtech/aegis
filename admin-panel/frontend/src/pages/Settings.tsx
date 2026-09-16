import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import DataTable from '../components/DataTable';

// Raw key/value settings editor. Moved off the Overview landing page so the
// first thing a user sees isn't a config table.
// A setting's stored value as text, and its current text (the pending edit if
// there is one). Two cells read them, so they are not derived per cell.
const rawValue = (s: any): string =>
  typeof s.value === 'string' ? s.value : JSON.stringify(s.value, null, 2);

const editedValue = (s: any, edits: Record<string, string>): string =>
  edits[s.key] ?? rawValue(s);

const isDirty = (s: any, edits: Record<string, string>): boolean =>
  edits[s.key] !== undefined && edits[s.key] !== rawValue(s);

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
        <DataTable
          rows={settings}
          rowKey={s => s.key}
          emptyText={loading ? undefined : 'No settings configured.'}
          columns={[
            {
              header: 'Key',
              th: { style: { width: '20%' } },
              cell: s => <code style={{ wordBreak: 'break-all' }}>{s.key}</code>,
            },
            {
              header: 'Value',
              cell: s => {
                const current = editedValue(s, edits);
                // Multi-line editor for anything long enough that a single
                // input would hide the body.
                return current.length > 60 || current.includes('\n') ? (
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
                );
              },
            },
            {
              header: 'Updated',
              th: { style: { width: 180 } },
              td: { className: 'meta' },
              cell: s => (s.updated_at ? new Date(s.updated_at).toLocaleString() : '—'),
            },
            {
              th: { style: { width: 80 } },
              cell: s => isDirty(s, edits) && (
                <button className="btn btn-sm btn-primary" onClick={() => void saveSetting(s.key)}>Save</button>
              ),
            },
          ]}
        />
      </div>
    </div>
  );
}
