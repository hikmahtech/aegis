import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

interface DbStatus {
  status?: string;
  latency_ms?: number;
  error?: string;
}

interface TemporalStatus {
  status?: 'ok' | 'error' | 'unknown';
  error?: string;
  note?: string;
}

interface SystemStatus {
  status?: 'ok' | 'degraded';
  auth_mode?: 'disabled' | 'basic' | 'api_key' | 'basic+api_key' | 'none';
  db?: DbStatus;
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
            Live health of AEGIS's own backing services: the database and Temporal.
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

      {data?.auth_mode === 'disabled' && (
        <div
          style={{
            background: 'var(--danger-tint)',
            border: '1px solid var(--danger)',
            color: 'var(--danger-text)',
            padding: '10px 14px',
            margin: '8px 0 1rem',
            borderRadius: 'var(--radius-sm)',
          }}
        >
          <strong>⚠ Authentication is disabled</strong>
          <p style={{ margin: '0.4rem 0 0', fontSize: '0.9rem' }}>
            <code>AEGIS_AUTH_DISABLED=true</code> — every <code>/api</code> route accepts
            anonymous requests. This is only safe when an authenticating proxy fully fronts
            port 8080. If that port is published on the host, anyone who can reach it has
            full admin access. Set <code>AEGIS_ADMIN_USERNAME</code> /{' '}
            <code>AEGIS_ADMIN_PASSWORD</code> and remove the flag.
          </p>
        </div>
      )}

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
      </div>
    </div>
  );
}
