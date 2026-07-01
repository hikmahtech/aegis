import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

export default function Overview() {
  const [brief, setBrief] = useState<any>(null);
  const [status, setStatus] = useState<any>(null);
  const [info, setInfo] = useState<any>(null);
  const [settings, setSettings] = useState<any[]>([]);
  const [agents, setAgents] = useState<any[]>([]);
  const [config, setConfig] = useState<any>({});
  const [error, setError] = useState<Error | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);

  async function load() {
    setError(null); setLoading(true);
    try {
      const [b, s, i, set, ag, cfg] = await Promise.all([
        api.overviewBrief(),
        api.overviewStatus(),
        api.systemInfo(),
        api.listSettings(),
        api.listAgents(),
        api.getTemporalConfig(),
      ]);
      setBrief(b); setStatus(s); setInfo(i); setSettings(set); setAgents(ag); setConfig(cfg);
    } catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function saveSetting(key: string) {
    try {
      const raw = edits[key];
      let value: any = raw;
      // Try to parse as JSON; if that fails, store as string.
      try { value = JSON.parse(raw); } catch { /* keep as string */ }
      await api.updateSetting(key, value);
      setEdits(e => { const n = { ...e }; delete n[key]; return n; });
      await load();
    } catch (e: any) { setError(e); }
  }

  return (
    <div>
      <h1 className="page-title">Overview</h1>
      <p className="page-subtitle">What's happening right now?</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="grid">
        <div className="card" style={{ borderTop: '3px solid var(--primary)' }}>
          <h3>System</h3>
          <p>Version: {info?.version ?? '—'}</p>
          <p>Build: <code>{info?.git_sha ?? '—'}</code></p>
          <p>Uptime: {info?.uptime_seconds != null ? `${Math.round(info.uptime_seconds / 60)} min` : '—'}</p>
        </div>

        <div className="card" style={{ borderTop: '3px solid var(--warning)' }}>
          <h3>Pending work</h3>
          <p>Interactions: <strong>{brief?.pending_interactions ?? '—'}</strong></p>
          <p>Alerts (24h): <strong>{brief?.recent_alerts_24h ?? '—'}</strong></p>
        </div>

        <div className="card" style={{ borderTop: '3px solid var(--purple)' }}>
          <h3>Agents ({agents.length})</h3>
          {agents.map(a => (
            <p key={a.id}><Link to={`/personalities/${a.id}`}><strong>{a.name}</strong></Link> — {a.role}</p>
          ))}
        </div>

        <div className="card" style={{ borderTop: '3px solid var(--info)' }}>
          <h3>Last workflow runs</h3>
          {status?.last_workflow_runs?.length ? status.last_workflow_runs.slice(0, 5).map((r: any) => (
            <p key={r.workflow_type} style={{ fontSize: 13 }}>
              <strong>{r.workflow_type}</strong> — {r.last_run ? new Date(r.last_run).toLocaleString() : '—'}
            </p>
          )) : <p className="empty">No runs recorded yet.</p>}
        </div>

        <div className="card" style={{ borderTop: '3px solid var(--success)' }}>
          <h3>Quick links</h3>
          {config.temporal_ui_url && <p><a href={config.temporal_ui_url} target="_blank" rel="noopener">Temporal UI</a></p>}
          {config.n8n_ui_url && <p><a href={config.n8n_ui_url} target="_blank" rel="noopener">n8n</a></p>}
          <p><a href="https://litellm.example.com" target="_blank" rel="noopener">LiteLLM Dashboard</a></p>
          <p><Link to="/knowledge">Knowledge</Link></p>
        </div>
      </div>

      <h2 style={{ marginTop: 24 }}>Settings</h2>
      {loading && <p>Loading…</p>}
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
              // Use a textarea for anything long enough to benefit from multi-line
              // editing — single-line inputs hide most of the JSON body.
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
                        style={{ width: '100%', fontFamily: 'monospace', fontSize: 12, padding: 6 }}
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
                  <td style={{ fontSize: 12, color: '#666' }}>
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
