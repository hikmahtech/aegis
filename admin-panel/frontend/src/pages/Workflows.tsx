import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import DataTable from '../components/DataTable';

type Tab = 'live' | 'history';

// Temporal returns status as e.g. "WORKFLOW_EXECUTION_STATUS_COMPLETED". Strip
// the enum prefix and lowercase so it lines up with our CSS badge classes
// (badge-completed, badge-running, badge-failed, …).
function normalizeStatus(raw: unknown): string {
  if (raw == null) return 'running';
  return String(raw).replace(/^WORKFLOW_EXECUTION_STATUS_/i, '').toLowerCase();
}

// A Temporal execution carries its ids under two spellings depending on which
// API answered; both cells and the row key need them.
const wfIdOf = (e: any): string => e?.execution?.workflowId ?? e?.workflowId ?? '?';
const runIdOf = (e: any): string => e?.execution?.runId ?? e?.runId ?? '';

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
      {loading && executions.length === 0 && (
        <div className="skeleton-list" aria-busy="true" aria-label="Loading live workflows">
          {[0, 1, 2, 3, 4].map(i => <div key={i} className="skeleton skeleton-row" />)}
        </div>
      )}
      <div className="table-scroll">
        <DataTable
          rows={executions as any[]}
          rowKey={e => `${wfIdOf(e)}-${runIdOf(e)}`}
          emptyText={loading ? undefined : 'No live workflows.'}
          columns={[
            {
              header: 'Workflow ID',
              td: e => ({ className: 'mono', title: wfIdOf(e), style: { maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }),
              cell: e => (wfIdOf(e) !== '?'
                ? <Link to={`/workflows/${encodeURIComponent(wfIdOf(e))}?run=${encodeURIComponent(runIdOf(e))}`}>{wfIdOf(e)}</Link>
                : wfIdOf(e)),
            },
            { header: 'Type', cell: e => e?.type?.name ?? e?.workflowType?.name ?? '?' },
            {
              header: 'Status',
              cell: e => {
                const status = normalizeStatus(e?.status);
                return <span className={`badge badge-${status}`}>{status}</span>;
              },
            },
            {
              header: 'Start',
              cell: e => {
                const start = e?.startTime ?? e?.start_time;
                return start ? new Date(start).toLocaleString() : '—';
              },
            },
            {
              header: 'Link',
              cell: e => uiBase && wfIdOf(e) !== '?' && (
                <a href={`${uiBase}/namespaces/default/workflows/${wfIdOf(e)}/${runIdOf(e)}/history`}
                   target="_blank" rel="noreferrer">Temporal →</a>
              ),
            },
          ]}
        />
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
        <DataTable
          rows={rows}
          rowKey={r => r.run_id}
          emptyText={loading ? undefined : 'No runs match these filters.'}
          columns={[
            {
              header: 'run_id',
              td: r => ({ className: 'mono', title: r.run_id, style: { maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }),
              cell: r => (r.workflow_id
                ? <Link to={`/workflows/${encodeURIComponent(r.workflow_id)}?run=${encodeURIComponent(r.run_id ?? '')}`}>{(r.run_id ?? '').slice(0, 8)}…</Link>
                : `${(r.run_id ?? '').slice(0, 8)}…`),
            },
            { header: 'Type', cell: r => r.workflow_type },
            { header: 'Agent', cell: r => r.agent_id || '—' },
            {
              header: 'Status',
              cell: r => <span className={`badge badge-${String(r.status).toLowerCase()}`}>{r.status}</span>,
            },
            { header: 'Started', cell: r => (r.started_at ? new Date(r.started_at).toLocaleString() : '—') },
            { header: 'Duration', cell: r => (r.duration_ms != null ? `${r.duration_ms} ms` : '—') },
            {
              header: 'Error',
              td: r => ({ title: r.error || '', style: { maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }),
              cell: r => r.error || '—',
            },
          ]}
        />
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
