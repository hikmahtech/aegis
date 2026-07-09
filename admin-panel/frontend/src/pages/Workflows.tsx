import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type Tab = 'live' | 'history';

// Temporal returns status as e.g. "WORKFLOW_EXECUTION_STATUS_COMPLETED". Strip
// the enum prefix and lowercase so it lines up with our CSS badge classes
// (badge-completed, badge-running, badge-failed, …).
function normalizeStatus(raw: unknown): string {
  if (raw == null) return 'running';
  return String(raw).replace(/^WORKFLOW_EXECUTION_STATUS_/i, '').toLowerCase();
}

const LIVE_POLL_MS = 5_000;
const HISTORY_PAGE_SIZE = 50;

export default function Workflows() {
  const [tab, setTab] = useState<Tab>('live');

  return (
    <div>
      <h1 className="page-title">Workflows</h1>
      <p className="page-subtitle">Recent Temporal executions (auto-refreshing) and full historical run log.</p>

      <div className="filter-bar">
        <button className={`btn ${tab === 'live' ? 'active' : ''}`} onClick={() => setTab('live')}>Recent</button>
        <button className={`btn ${tab === 'history' ? 'active' : ''}`} onClick={() => setTab('history')}>History</button>
      </div>

      {tab === 'live' ? <LiveTab /> : <HistoryTab />}
    </div>
  );
}

function LiveTab() {
  const [data, setData] = useState<any>({ executions: [] });
  const [temporalCfg, setTemporalCfg] = useState<any>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const inflightRef = useRef(false);

  async function load() {
    if (inflightRef.current) return;
    inflightRef.current = true;
    try {
      const r = await api.listTemporalWorkflows(30);
      setData(r || { executions: [] });
    } catch (e: any) {
      setError(e);
    } finally {
      inflightRef.current = false;
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    api.getTemporalConfig().then(setTemporalCfg).catch(() => {});
    const id = setInterval(load, LIVE_POLL_MS);
    return () => clearInterval(id);
  }, []);

  const uiBase: string | null = temporalCfg?.temporal_ui_url
    ? String(temporalCfg.temporal_ui_url).replace(/\/$/, '')
    : null;

  const executions: any[] = Array.isArray(data?.executions) ? data.executions : [];

  return (
    <>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {data?.error && <div className="empty">{data.error}</div>}
      {loading && executions.length === 0 && <div className="loading">Loading live workflows…</div>}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Workflow ID</th>
              <th>Type</th>
              <th>Status</th>
              <th>Start</th>
              <th>Link</th>
            </tr>
          </thead>
          <tbody>
            {executions.map((e: any) => {
              const wfId = e?.execution?.workflowId ?? e?.workflowId ?? '?';
              const runId = e?.execution?.runId ?? e?.runId ?? '';
              const type = e?.type?.name ?? e?.workflowType?.name ?? '?';
              const status = normalizeStatus(e?.status);
              const start = e?.startTime ?? e?.start_time;
              return (
                <tr key={`${wfId}-${runId}`}>
                  <td className="mono" title={wfId} style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {wfId !== '?'
                      ? <Link to={`/workflows/${encodeURIComponent(wfId)}?run=${encodeURIComponent(runId)}`}>{wfId}</Link>
                      : wfId}
                  </td>
                  <td>{type}</td>
                  <td><span className={`badge badge-${status}`}>{status}</span></td>
                  <td>{start ? new Date(start).toLocaleString() : '—'}</td>
                  <td>
                    {uiBase && wfId !== '?' && (
                      <a href={`${uiBase}/namespaces/default/workflows/${wfId}/${runId}/history`}
                         target="_blank" rel="noreferrer">Temporal →</a>
                    )}
                  </td>
                </tr>
              );
            })}
            {executions.length === 0 && !loading && (
              <tr><td colSpan={5} className="empty">No live workflows.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}

function HistoryTab() {
  const [rows, setRows] = useState<any[]>([]);
  const [agents, setAgents] = useState<Array<{ id: string; name: string }>>([]);
  const [agentFilter, setAgentFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const inflightRef = useRef(false);

  useEffect(() => {
    api.listAgents().then(setAgents).catch(() => setAgents([]));
  }, []);

  async function load(freshOffset: number) {
    if (inflightRef.current) return;
    inflightRef.current = true;
    setLoading(true);
    try {
      const data = await api.listWorkflowRuns({
        agent_id: agentFilter || undefined,
        workflow_type: typeFilter || undefined,
        status: statusFilter || undefined,
        limit: HISTORY_PAGE_SIZE,
        offset: freshOffset,
      });
      if (freshOffset === 0) setRows(data || []);
      else setRows(prev => [...prev, ...(data || [])]);
      setHasMore((data || []).length === HISTORY_PAGE_SIZE);
    } catch (e: any) {
      setError(e);
    } finally {
      inflightRef.current = false;
      setLoading(false);
    }
  }

  useEffect(() => {
    setOffset(0);
    void load(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentFilter, typeFilter, statusFilter]);

  function loadMore() {
    const next = offset + HISTORY_PAGE_SIZE;
    setOffset(next);
    void load(next);
  }

  return (
    <>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <div className="filter-bar">
        <select value={agentFilter} onChange={e => setAgentFilter(e.target.value)}>
          <option value="">All agents</option>
          {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <input
          value={typeFilter}
          onChange={e => setTypeFilter(e.target.value)}
          placeholder="workflow_type exact match"
          style={{ flex: 1, minWidth: 200 }}
        />
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
          <option value="">All statuses</option>
          <option value="completed">completed</option>
          <option value="failed">failed</option>
          <option value="running">running</option>
          <option value="timed_out">timed_out</option>
          <option value="terminated">terminated</option>
          <option value="canceled">canceled</option>
        </select>
      </div>

      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>run_id</th>
              <th>Type</th>
              <th>Agent</th>
              <th>Status</th>
              <th>Started</th>
              <th>Duration</th>
              <th>Error</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.run_id}>
                <td className="mono" title={r.run_id} style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.workflow_id
                    ? <Link to={`/workflows/${encodeURIComponent(r.workflow_id)}?run=${encodeURIComponent(r.run_id ?? '')}`}>{(r.run_id ?? '').slice(0, 8)}…</Link>
                    : `${(r.run_id ?? '').slice(0, 8)}…`}
                </td>
                <td>{r.workflow_type}</td>
                <td>{r.agent_id || '—'}</td>
                <td><span className={`badge badge-${String(r.status).toLowerCase()}`}>{r.status}</span></td>
                <td>{r.started_at ? new Date(r.started_at).toLocaleString() : '—'}</td>
                <td>{r.duration_ms != null ? `${r.duration_ms} ms` : '—'}</td>
                <td title={r.error || ''} style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.error || '—'}
                </td>
              </tr>
            ))}
            {rows.length === 0 && !loading && (
              <tr><td colSpan={7} className="empty">No runs match these filters.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      {hasMore && (
        <div style={{ marginTop: 12 }}>
          <button className="btn" disabled={loading} onClick={loadMore}>
            {loading ? 'Loading…' : 'Load more'}
          </button>
        </div>
      )}
    </>
  );
}
