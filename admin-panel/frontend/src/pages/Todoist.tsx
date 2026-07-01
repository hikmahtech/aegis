import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type TodoistState = {
  sync: { key: string; last_full_sync_at: string | null; last_incremental_at: string | null } | null;
  outbox: {
    counts: Record<string, number>;
    oldest_pending_age_seconds: number | null;
    failed_recent: any[];
  };
  tasks: { open: number; completed_7d: number; pending_clarify: number };
  managed_projects: Record<string, string> | null;
};

function fmtAge(seconds: number | null): string {
  if (seconds == null) return '—';
  if (seconds < 120) return `${seconds}s`;
  if (seconds < 7200) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

const _PROJECT_KEYS = ['inbox', 'next', 'someday'] as const;

export default function Todoist() {
  const [data, setData] = useState<TodoistState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [cfg, setCfg] = useState<any>(null);
  const [apiKey, setApiKey] = useState('');
  const [projects, setProjects] = useState<Record<string, string>>({ inbox: '', next: '', someday: '' });
  const [savingCfg, setSavingCfg] = useState(false);
  const [cfgMsg, setCfgMsg] = useState('');
  const [gtd, setGtd] = useState<any>(null);
  const [savingGtd, setSavingGtd] = useState(false);
  const [gtdMsg, setGtdMsg] = useState('');

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setData(await api.todoistState());
      const c = await api.getTodoistConfig();
      setCfg(c);
      setProjects({ inbox: '', next: '', someday: '', ...(c.projects || {}) });
      setApiKey('');
      setGtd(await api.getGtdRules());
    } catch (e: any) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  async function saveGtd() {
    setSavingGtd(true); setGtdMsg(''); setError(null);
    try {
      const skip: Record<string, string> = {};
      for (const [k, v] of Object.entries(gtd.skip_inbox || {})) if (v) skip[k] = v as string;
      const r = await api.saveGtdRules({ assignee: gtd.assignee, contexts: gtd.contexts, skip_inbox: skip });
      setGtd(r);
      setGtdMsg('Saved — applies within ~30s.');
    } catch (e: any) { setError(e); } finally { setSavingGtd(false); }
  }

  async function saveConfig() {
    setSavingCfg(true); setCfgMsg(''); setError(null);
    try {
      const body: any = { projects };
      if (apiKey) body.api_key = apiKey;
      const c = await api.saveTodoistConfig(body);
      setCfg(c); setApiKey('');
      setCfgMsg('Saved — restart the worker to apply a new API key to flows.');
    } catch (e: any) { setError(e); } finally { setSavingCfg(false); }
  }

  useEffect(() => { void load(); }, []);

  if (loading && !data) return <div className="loading">Loading Todoist state…</div>;

  const failedCount = data?.outbox?.counts?.failed ?? 0;
  const pendingCount = data?.outbox?.counts?.pending ?? 0;

  return (
    <div>
      <h1 className="page-title">Todoist</h1>
      <p className="page-subtitle">GTD hub health — sync watermarks, projection counts, write outbox.</p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ marginTop: 16 }}>
        <h3>Configure Todoist</h3>
        <p className="page-subtitle">
          Your Todoist API token + the project ids AEGIS manages as GTD buckets.
          {cfg ? ` API key: ${cfg.api_key_set ? `set (${cfg.source})` : 'not set'}.` : ''}
        </p>
        <div className="cfg-row">
          <span className="cfg-label">API token</span>
          <input type="password" value={apiKey} onChange={e => setApiKey(e.target.value)}
            placeholder={cfg?.api_key_set ? '•••••••• (set — leave blank to keep)' : 'Todoist API token'} />
        </div>
        {_PROJECT_KEYS.map(k => (
          <div key={k} className="cfg-row">
            <span className="cfg-label" style={{ textTransform: 'capitalize' }}>{k} project</span>
            <input value={projects[k] || ''}
              onChange={e => setProjects({ ...projects, [k]: e.target.value })} placeholder={`${k} project id`} />
          </div>
        ))}
        <button className="btn" disabled={savingCfg} onClick={saveConfig}>{savingCfg ? 'Saving…' : 'Save Todoist config'}</button>
        {cfgMsg && <span className="msg-success" style={{ marginLeft: 10 }}>{cfgMsg}</span>}
      </div>

      {gtd && (
        <div className="card" style={{ marginTop: 16 }}>
          <h3>GTD clarify rules</h3>
          <p className="page-subtitle">
            How captured items are auto-labelled by source tag — assignee, context labels, and
            skip-inbox routing. (The @sebas/@raphael/@maou/@pandora agent routing stays in code.)
          </p>
          <div className="table-scroll">
          <table style={{ width: '100%', fontSize: 13 }}>
            <thead><tr>
              <th style={{ textAlign: 'left' }}>Source</th><th>Assignee</th>
              <th>Contexts (comma-sep)</th><th>Skip-inbox →</th>
            </tr></thead>
            <tbody>
              {(gtd.source_tags || []).map((tag: string) => (
                <tr key={tag}>
                  <td><code>{tag}</code></td>
                  <td><input style={{ width: 90 }} value={gtd.assignee?.[tag] || ''}
                    onChange={e => setGtd({ ...gtd, assignee: { ...gtd.assignee, [tag]: e.target.value } })} /></td>
                  <td><input style={{ width: '100%' }} value={(gtd.contexts?.[tag] || []).join(', ')}
                    onChange={e => setGtd({ ...gtd, contexts: { ...gtd.contexts, [tag]: e.target.value.split(',').map(s => s.trim()).filter(Boolean) } })} /></td>
                  <td><input style={{ width: 90 }} placeholder="(none)" value={gtd.skip_inbox?.[tag] || ''}
                    onChange={e => setGtd({ ...gtd, skip_inbox: { ...gtd.skip_inbox, [tag]: e.target.value } })} /></td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
          <button className="btn" style={{ marginTop: 8 }} disabled={savingGtd} onClick={saveGtd}>
            {savingGtd ? 'Saving…' : 'Save GTD rules'}
          </button>
          {gtdMsg && <span className="msg-success" style={{ marginLeft: 10 }}>{gtdMsg}</span>}
        </div>
      )}

      {/* Summary cards */}
      <section style={{ marginTop: 24, display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Open tasks</div>
          <div style={{ fontSize: 24, fontWeight: 600 }}>{data?.tasks?.open ?? '—'}</div>
        </div>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Completed (7d)</div>
          <div style={{ fontSize: 24, fontWeight: 600 }}>{data?.tasks?.completed_7d ?? '—'}</div>
        </div>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Pending clarify</div>
          <div style={{ fontSize: 24, fontWeight: 600 }}>{data?.tasks?.pending_clarify ?? '—'}</div>
        </div>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Outbox pending</div>
          <div style={{ fontSize: 24, fontWeight: 600 }}>
            {pendingCount}
            {pendingCount > 0 && (
              <span style={{ fontSize: 12, marginLeft: 8, opacity: 0.7 }}>
                oldest {fmtAge(data?.outbox?.oldest_pending_age_seconds ?? null)}
              </span>
            )}
          </div>
        </div>
        <div className="card" style={{ padding: 16, minWidth: 160 }}>
          <div style={{ fontSize: 12, opacity: 0.7 }}>Outbox failed</div>
          <div style={{ fontSize: 24, fontWeight: 600, color: failedCount > 0 ? 'var(--error, #c0392b)' : undefined }}>
            {failedCount}
          </div>
        </div>
      </section>

      {/* Sync watermarks */}
      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>Sync state</h2>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Last full sync</th>
                <th>Last incremental</th>
                <th>Managed projects</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>{data?.sync?.last_full_sync_at ? new Date(data.sync.last_full_sync_at).toLocaleString() : '—'}</td>
                <td>{data?.sync?.last_incremental_at ? new Date(data.sync.last_incremental_at).toLocaleString() : '—'}</td>
                <td style={{ wordBreak: 'break-word' }}>
                  {data?.managed_projects
                    ? Object.entries(data.managed_projects).map(([k, v]) => `${k}: ${v}`).join(' · ')
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      {/* Failed outbox commands — lost writes */}
      <section style={{ marginTop: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>
          Failed outbox commands {failedCount > 0 && <span className="badge badge-error">lost writes</span>}
        </h2>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Command</th>
                <th>Attempts</th>
                <th>Last attempt</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {(data?.outbox?.failed_recent?.length ?? 0) === 0 && (
                <tr><td colSpan={5} className="empty">No failed commands ✨</td></tr>
              )}
              {data?.outbox?.failed_recent?.map((r: any) => (
                <tr key={r.id}>
                  <td>{r.id}</td>
                  <td><strong>{r.command_type}</strong></td>
                  <td>{r.attempt_count}</td>
                  <td>{r.last_attempt_at ? new Date(r.last_attempt_at).toLocaleString() : '—'}</td>
                  <td>{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
