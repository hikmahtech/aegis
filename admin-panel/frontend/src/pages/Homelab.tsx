import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type HomelabState = {
  drift: any[];
  backups: any[];
  schedules: any[];
  certs: any[];
};

export default function Homelab() {
  const [data, setData] = useState<HomelabState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [running, setRunning] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setData(await api.homelabState());
    } catch (e: any) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  async function recheck(flow: string) {
    setRunning(flow);
    try {
      await api.homelabRunFlow(flow);
      await load();
    } catch (e: any) {
      setError(e);
    } finally {
      setRunning(null);
    }
  }

  useEffect(() => { void load(); }, []);

  if (loading && !data) return <div className="loading">Loading homelab state…</div>;

  return (
    <div>
      <h1 className="page-title">Homelab Guardian</h1>
      <p className="page-subtitle">Service drift, backup health, schedule health, TLS certs.</p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {/* Service Drift */}
      <section style={{ marginTop: 24 }}>
        <div className="filter-bar" style={{ justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Service drift (latest 50)</h2>
          <button
            className="btn"
            disabled={running === 'service_drift'}
            onClick={() => void recheck('service_drift')}
          >
            {running === 'service_drift' ? 'Running…' : '↻ Re-check'}
          </button>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Service</th>
                <th>Type</th>
                <th>Severity</th>
                <th>Detected</th>
                <th>Resolved</th>
              </tr>
            </thead>
            <tbody>
              {data?.drift?.length === 0 && (
                <tr><td colSpan={5} className="empty">No drift records</td></tr>
              )}
              {data?.drift?.map((d: any) => (
                <tr key={d.id} style={{ opacity: d.resolved_at ? 0.5 : 1 }}>
                  <td><strong>{d.service_name}</strong></td>
                  <td>{d.drift_type}</td>
                  <td>
                    <span className={`badge badge-${d.severity === 'critical' ? 'error' : d.severity === 'warning' ? 'warning' : 'info'}`}>
                      {d.severity}
                    </span>
                  </td>
                  <td>{d.detected_at ? new Date(d.detected_at).toLocaleString() : '—'}</td>
                  <td>{d.resolved_at ? new Date(d.resolved_at).toLocaleString() : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Backup Health */}
      <section style={{ marginTop: 24 }}>
        <div className="filter-bar" style={{ justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Backup health</h2>
          <button
            className="btn"
            disabled={running === 'backup_audit'}
            onClick={() => void recheck('backup_audit')}
          >
            {running === 'backup_audit' ? 'Running…' : '↻ Re-check'}
          </button>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Set</th>
                <th>Last backup</th>
                <th>Size</th>
                <th>Delta %</th>
                <th>Drill</th>
              </tr>
            </thead>
            <tbody>
              {data?.backups?.length === 0 && (
                <tr><td colSpan={5} className="empty">No backup records</td></tr>
              )}
              {data?.backups?.map((b: any) => (
                <tr key={b.backup_set}>
                  <td><strong>{b.backup_set}</strong></td>
                  <td>{b.last_backup_at ? new Date(b.last_backup_at).toLocaleString() : '—'}</td>
                  <td className="mono">{b.size_bytes ? (b.size_bytes / 1e6).toFixed(1) + ' MB' : '—'}</td>
                  <td className="mono">{b.size_delta_pct != null ? b.size_delta_pct + '%' : '—'}</td>
                  <td>
                    {b.restore_drill_ok === null || b.restore_drill_ok === undefined
                      ? '—'
                      : b.restore_drill_ok
                        ? <span className="badge badge-success">OK</span>
                        : <span className="badge badge-error">FAIL</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Schedule Health */}
      <section style={{ marginTop: 24 }}>
        <div className="filter-bar" style={{ justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Schedules</h2>
          <button
            className="btn"
            disabled={running === 'schedule_health'}
            onClick={() => void recheck('schedule_health')}
          >
            {running === 'schedule_health' ? 'Running…' : '↻ Re-check'}
          </button>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Name</th>
                <th>Expected</th>
                <th>Actual</th>
                <th>Failures</th>
              </tr>
            </thead>
            <tbody>
              {data?.schedules?.length === 0 && (
                <tr><td colSpan={5} className="empty">No schedule records</td></tr>
              )}
              {data?.schedules?.map((s: any) => (
                <tr
                  key={`${s.source}:${s.schedule_name}`}
                  style={{ color: s.actual_status !== s.expected_status ? 'var(--danger, #e53e3e)' : undefined }}
                >
                  <td>{s.source}</td>
                  <td><strong>{s.schedule_name}</strong></td>
                  <td>{s.expected_status}</td>
                  <td>{s.actual_status}</td>
                  <td className="mono">{s.consecutive_failures ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* TLS Certs */}
      <section style={{ marginTop: 24 }}>
        <div className="filter-bar" style={{ justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>TLS certs</h2>
          <button
            className="btn"
            disabled={running === 'cert_radar'}
            onClick={() => void recheck('cert_radar')}
          >
            {running === 'cert_radar' ? 'Running…' : '↻ Re-check'}
          </button>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Domain</th>
                <th>Expires</th>
                <th>Days left</th>
                <th>Last alert threshold</th>
              </tr>
            </thead>
            <tbody>
              {data?.certs?.length === 0 && (
                <tr><td colSpan={4} className="empty">No cert records</td></tr>
              )}
              {data?.certs?.map((c: any) => (
                <tr
                  key={c.domain}
                  style={{ color: c.days_until_expiry <= 14 ? 'var(--danger, #e53e3e)' : undefined }}
                >
                  <td><strong>{c.domain}</strong></td>
                  <td>{c.not_after ? new Date(c.not_after).toLocaleDateString() : '—'}</td>
                  <td className="mono">{c.days_until_expiry ?? '—'}</td>
                  <td>{c.last_alert_threshold ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
