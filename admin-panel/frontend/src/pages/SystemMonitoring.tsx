import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

interface DbStatus {
  status?: string;
  latency_ms?: number;
  error?: string;
}

interface ServiceRow {
  name: string;
  stack?: string;
  replicas?: string;
  image?: string;
}

interface ServicesStatus {
  status?: 'ok' | 'error' | 'unconfigured';
  services?: ServiceRow[];
  infra_slug?: string;
  note?: string;
  error?: string;
}

interface TemporalStatus {
  status?: 'ok' | 'error' | 'unknown';
  error?: string;
  note?: string;
}

interface SystemStatus {
  status?: 'ok' | 'degraded';
  db?: DbStatus;
  services?: ServicesStatus;
  temporal?: TemporalStatus;
}

function overallBadgeClass(status?: string) {
  if (status === 'ok') return 'badge badge-success';
  if (status === 'degraded') return 'badge badge-error';
  return 'badge badge-neutral';
}

function probeBadgeClass(status?: string) {
  if (status === 'ok') return 'badge badge-success';
  if (status === 'error') return 'badge badge-error';
  if (status === 'unconfigured' || status === 'unknown') return 'badge badge-neutral';
  return 'badge badge-neutral';
}

function replicaBadgeClass(replicas?: string) {
  if (!replicas) return 'badge badge-neutral';
  const match = replicas.match(/^(\d+)\/(\d+)$/);
  if (match) {
    const [, running, desired] = match;
    return running === desired ? 'badge badge-success' : 'badge badge-error';
  }
  return 'badge badge-neutral';
}

export default function SystemMonitoring() {
  const [data, setData] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [rechecking, setRechecking] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  async function load(isRecheck = false) {
    if (isRecheck) setRechecking(true); else setLoading(true);
    setError(null);
    try {
      const result = await api.systemStatus();
      setData(result);
    } catch (e: any) {
      setError(e);
    } finally {
      if (isRecheck) setRechecking(false); else setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  if (loading && !data) return <div className="loading">Loading system status...</div>;

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">System monitoring</h1>
          <p className="page-subtitle">
            Live health of AEGIS's own running services — database, container services, and Temporal.
            Requires an infrastructure entry flagged &ldquo;hosts AEGIS&rdquo; on the{' '}
            <Link to="/infra">Infrastructure</Link> page to detect where AEGIS itself runs.
          </p>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
          {data?.status && <span className={overallBadgeClass(data.status)}>{data.status}</span>}
          <button className="btn" disabled={rechecking} onClick={() => void load(true)}>
            {rechecking ? 'Checking...' : '↻ Re-check'}
          </button>
        </div>
      </div>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '1rem' }}>
        {/* Database */}
        <div className="card">
          <div className="section-header-row" style={{ marginBottom: '0.6rem' }}>
            <h3 style={{ margin: 0 }}>Database</h3>
            <span className={probeBadgeClass(data?.db?.status)}>{data?.db?.status || 'unknown'}</span>
          </div>
          {data?.db?.error ? (
            <p className="msg-error">{data.db.error}</p>
          ) : (
            <div className="cfg-row">
              <span className="cfg-label">Latency</span>
              <span className="meta mono">{data?.db?.latency_ms != null ? `${data.db.latency_ms} ms` : '—'}</span>
            </div>
          )}
        </div>

        {/* Temporal */}
        <div className="card">
          <div className="section-header-row" style={{ marginBottom: '0.6rem' }}>
            <h3 style={{ margin: 0 }}>Temporal</h3>
            <span className={probeBadgeClass(data?.temporal?.status)}>{data?.temporal?.status || 'unknown'}</span>
          </div>
          {data?.temporal?.error ? (
            <p className="msg-error">{data.temporal.error}</p>
          ) : data?.temporal?.note ? (
            <p className="meta">{data.temporal.note}</p>
          ) : (
            <p className="meta">Workflow engine reachable.</p>
          )}
        </div>

        {/* Services (spans full width since it can hold a table) */}
        <div className="card" style={{ gridColumn: '1 / -1' }}>
          <div className="section-header-row" style={{ marginBottom: '0.6rem' }}>
            <h3 style={{ margin: 0 }}>Services</h3>
            <span className={probeBadgeClass(data?.services?.status)}>{data?.services?.status || 'unknown'}</span>
          </div>

          {data?.services?.status === 'unconfigured' ? (
            <p className="meta">
              {data.services.note || 'No infrastructure entry is flagged as hosting AEGIS yet.'}{' '}
              Go to <Link to="/infra">Infrastructure</Link> and mark the host running AEGIS with &ldquo;This host runs AEGIS itself&rdquo;, then provision it.
            </p>
          ) : data?.services?.status === 'error' ? (
            <p className="msg-error">{data.services.error || 'Failed to query services.'}</p>
          ) : data?.services?.services && data.services.services.length > 0 ? (
            <>
              {data.services.infra_slug && (
                <p className="meta" style={{ marginBottom: '0.5rem' }}>Source: <span className="mono">{data.services.infra_slug}</span></p>
              )}
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Stack</th>
                      <th>Replicas</th>
                      <th>Image</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.services.services.map((s, i) => (
                      <tr key={`${s.name}-${i}`}>
                        <td><strong>{s.name}</strong></td>
                        <td>{s.stack || '—'}</td>
                        <td><span className={replicaBadgeClass(s.replicas)}>{s.replicas || '—'}</span></td>
                        <td className="mono" style={{ fontSize: 12, color: 'var(--text-muted)' }}>{s.image || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <div className="empty">No services reported.</div>
          )}
        </div>
      </div>
    </div>
  );
}
