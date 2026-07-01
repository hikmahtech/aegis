import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

const POLL_INTERVAL_MS = 10_000;

type Interaction = {
  id: string;
  agent_id: string;
  kind: string;
  origin: string;
  prompt: string;
  status: string;
  created_at: string;
  // Two shapes in the wild: `{choices: [...]}` (new style) and a flat
  // `{<choice_id>: <label>, …}` dict (alert path). extractChoices() handles both.
  options?: Record<string, unknown> | null;
};

type Agent = { id: string; name: string };

// Telegram-style HTML lives in prompts. The card shows only a 3-line snippet,
// so strip tags + decode entities to plain text (full HTML rendering is on the
// detail page).
function stripHtml(raw: string): string {
  if (!raw) return '';
  const doc = new DOMParser().parseFromString(raw, 'text/html');
  return doc.body.textContent || '';
}

function extractChoices(options: any): Array<{ id: string; label: string; description?: string }> {
  if (Array.isArray(options?.choices)) return options.choices;
  if (options && typeof options === 'object') {
    return Object.entries(options)
      .filter(([, v]) => typeof v === 'string')
      .map(([id, label]) => ({ id, label: String(label) }));
  }
  return [];
}

function relTime(iso: string): string {
  const then = new Date(iso).getTime();
  const diff = Math.max(0, Date.now() - then);
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export default function Interactions() {
  const [rows, setRows] = useState<Interaction[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentFilter, setAgentFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('pending');
  const [originFilter, setOriginFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [resolving, setResolving] = useState<string | null>(null); // interaction id
  const inflightRef = useRef(false);

  async function load(silent = false) {
    if (inflightRef.current) return;
    inflightRef.current = true;
    if (!silent) setLoading(true);
    try {
      const data = await api.listInteractions({
        agent_id: agentFilter || undefined,
        status: statusFilter || undefined,
        origin: originFilter || undefined,
        limit: 200,
      });
      setRows(data || []);
    } catch (e: any) {
      setError(e);
    } finally {
      inflightRef.current = false;
      if (!silent) setLoading(false);
    }
  }

  useEffect(() => {
    api.listAgents().then(setAgents).catch(() => setAgents([]));
  }, []);

  useEffect(() => {
    void load();
    const id = setInterval(() => { void load(true); }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentFilter, statusFilter, originFilter]);

  async function resolve(id: string, value: string) {
    setResolving(id);
    try {
      await api.resolveInteraction(id, { value });
      setRows(r => r.filter(x => x.id !== id));
    } catch (e: any) {
      setError(e);
    } finally {
      setResolving(null);
    }
  }

  const agentName = (id: string) => agents.find(a => a.id === id)?.name || id;

  return (
    <div>
      <h1 className="page-title">Interactions</h1>
      <p className="page-subtitle">Pending handoffs from workflows — approve, choose, ack, or open for input</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="filter-bar">
        <select value={agentFilter} onChange={e => setAgentFilter(e.target.value)}>
          <option value="">All agents</option>
          {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
          <option value="pending">Pending</option>
          <option value="resolved">Resolved</option>
          <option value="archived">Archived</option>
          <option value="">All</option>
        </select>
        <input
          value={originFilter}
          onChange={e => setOriginFilter(e.target.value)}
          placeholder="origin (comma-separated)"
          style={{ flex: 1, minWidth: 160 }}
        />
        <span className="meta" style={{ alignSelf: 'center' }}>{rows.length} rows · auto-refresh 10s</span>
      </div>

      {loading && rows.length === 0 && <div className="loading">Loading interactions…</div>}
      {!loading && rows.length === 0 && <div className="empty">No interactions match these filters.</div>}

      <div style={{ display: 'grid', gap: 10 }}>
        {rows.map(r => (
          <div key={r.id} className="card" style={{ display: 'flex', gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 13, marginBottom: 4, flexWrap: 'wrap' }}>
                <strong>{agentName(r.agent_id)}</strong>
                <span className="badge badge-type">{r.kind}</span>
                <span className="meta-tag">{r.origin}</span>
                <span className="meta" style={{ marginLeft: 'auto' }}>{relTime(r.created_at)}</span>
              </div>
              <Link
                to={`/interactions/${r.id}`}
                style={{ display: 'block', color: 'inherit', textDecoration: 'none' }}
              >
                <p style={{
                  margin: 0, fontSize: 13, lineHeight: 1.4,
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                  display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
                  overflow: 'hidden',
                }}>{stripHtml(r.prompt)}</p>
              </Link>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {renderInlineActions(r, resolve, resolving === r.id)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function renderInlineActions(
  r: Interaction,
  resolve: (id: string, value: string) => void,
  busy: boolean,
) {
  if (r.status !== 'pending') {
    return <span className="meta">{r.status}</span>;
  }
  const stop = (e: React.MouseEvent) => { e.preventDefault(); e.stopPropagation(); };
  switch (r.kind) {
    case 'approval':
      return (
        <>
          <button className="btn btn-primary btn-sm" disabled={busy}
            onClick={e => { stop(e); resolve(r.id, 'approved'); }}>Approve</button>
          <button className="btn btn-sm" disabled={busy}
            onClick={e => { stop(e); resolve(r.id, 'rejected'); }}>Reject</button>
        </>
      );
    case 'choice': {
      const choices = extractChoices(r.options);
      if (choices.length === 0) {
        return <Link to={`/interactions/${r.id}`} className="btn btn-sm" onClick={stop}>Open →</Link>;
      }
      return (
        <>
          {choices.map(c => (
            <button key={c.id} className="btn btn-sm" disabled={busy}
              onClick={e => { stop(e); resolve(r.id, c.id); }}
              title={c.description}>{c.label}</button>
          ))}
        </>
      );
    }
    case 'ack':
      return (
        <button className="btn btn-primary btn-sm" disabled={busy}
          onClick={e => { stop(e); resolve(r.id, 'ack'); }}>Acknowledge</button>
      );
    default:
      // input, draft_review — heavy kinds go to the detail page
      return <Link to={`/interactions/${r.id}`} className="btn btn-sm" onClick={stop}>Open →</Link>;
  }
}
