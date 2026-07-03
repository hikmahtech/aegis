import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

// Prompts can carry light chat HTML; the card shows plain-text snippets.
function stripHtml(raw: string): string {
  if (!raw) return '';
  const doc = new DOMParser().parseFromString(raw, 'text/html');
  return doc.body.textContent || '';
}

function relTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!then) return '';
  const s = Math.floor(Math.max(0, Date.now() - then) / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function Overview() {
  const [brief, setBrief] = useState<any>(null);
  const [status, setStatus] = useState<any>(null);
  const [info, setInfo] = useState<any>(null);
  const [agents, setAgents] = useState<any[]>([]);
  const [config, setConfig] = useState<any>({});
  const [pending, setPending] = useState<any[]>([]);
  const [error, setError] = useState<Error | null>(null);

  async function load() {
    setError(null);
    try {
      const [b, s, i, ag, cfg, pend] = await Promise.all([
        api.overviewBrief(),
        api.overviewStatus(),
        api.systemInfo(),
        api.listAgents(),
        api.getTemporalConfig(),
        api.listInteractions({ status: 'pending', limit: 6 }),
      ]);
      setBrief(b); setStatus(s); setInfo(i); setAgents(ag); setConfig(cfg); setPending(pend || []);
    } catch (e: any) { setError(e); }
  }
  useEffect(() => { void load(); }, []);

  const agentName = (id: string) => agents.find(a => a.id === id)?.name || id;
  const uptime = info?.uptime_seconds != null ? `${Math.round(info.uptime_seconds / 60)}m` : '—';

  return (
    <div>
      <h1 className="page-title">Overview</h1>
      <p className="page-subtitle">What needs you right now, and how the system is doing.</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="stats-bar">
        <div className="stat-item">
          <span className="stat-value">{brief?.pending_interactions ?? '—'}</span>
          <span className="stat-label">Pending decisions</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{brief?.recent_alerts_24h ?? '—'}</span>
          <span className="stat-label">Alerts · 24h</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{agents.length || '—'}</span>
          <span className="stat-label">Agents</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{uptime}</span>
          <span className="stat-label">Uptime</span>
        </div>
      </div>

      {/* Decision-first: the whole point of AEGIS is surfacing what needs a human. */}
      <div className="section">
        <div className="section-header-row">
          <h2 className="section-title">
            Needs your decision
            {pending.length > 0 && <span className="count-badge">{pending.length}</span>}
          </h2>
          {pending.length > 0 && <Link to="/interactions" className="btn btn-sm">View all →</Link>}
        </div>
        {pending.length === 0 ? (
          <div className="empty">All clear — nothing needs you right now.</div>
        ) : (
          <div className="decision-list">
            {pending.map(p => (
              <Link key={p.id} to={`/interactions/${p.id}`} className="decision-item">
                <div className="decision-body">
                  <div className="decision-title">{stripHtml(p.prompt) || p.kind}</div>
                  <div className="decision-meta">
                    {agentName(p.agent_id)} · {p.kind} · {relTime(p.created_at)}
                  </div>
                </div>
                <span className="decision-arrow">→</span>
              </Link>
            ))}
          </div>
        )}
      </div>

      <div className="grid">
        <div className="card">
          <h3>System</h3>
          <p>Version: <strong>{info?.version ?? '—'}</strong></p>
          <p>Build: <code>{info?.git_sha ?? '—'}</code></p>
          <p>Uptime: <strong>{uptime}</strong></p>
        </div>

        <div className="card">
          <h3>Agents ({agents.length})</h3>
          {agents.length === 0 && <p className="empty" style={{ padding: '0.5rem 0' }}>No agents yet.</p>}
          {agents.map(a => (
            <p key={a.id}><Link to={`/agents/${a.id}`}><strong>{a.name}</strong></Link> — {a.role}</p>
          ))}
        </div>

        <div className="card">
          <h3>Recent workflow runs</h3>
          {status?.last_workflow_runs?.length ? status.last_workflow_runs.slice(0, 5).map((r: any) => (
            <p key={r.workflow_type}>
              <strong>{r.workflow_type}</strong> — {r.last_run ? new Date(r.last_run).toLocaleString() : '—'}
            </p>
          )) : <p className="empty" style={{ padding: '0.5rem 0' }}>No runs recorded yet.</p>}
        </div>

        <div className="card">
          <h3>Quick links</h3>
          {config.temporal_ui_url && <p><a href={config.temporal_ui_url} target="_blank" rel="noopener">Temporal UI ↗</a></p>}
          {config.postiz_ui_url && <p><a href={config.postiz_ui_url} target="_blank" rel="noopener">Postiz ↗</a></p>}
          {config.n8n_ui_url && <p><a href={config.n8n_ui_url} target="_blank" rel="noopener">n8n ↗</a></p>}
          <p><Link to="/knowledge">Knowledge</Link></p>
          <p><Link to="/settings">Settings</Link></p>
        </div>
      </div>
    </div>
  );
}
