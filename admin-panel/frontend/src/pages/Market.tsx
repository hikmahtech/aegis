import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import JsonViewer from '../components/JsonViewer';

export default function Market() {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setError(null); setLoading(true);
    try { setData(await api.marketSummary()); }
    catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  const indices: any[] = Array.isArray(data?.indices) ? data.indices : [];

  return (
    <div>
      <h1 className="page-title">Market</h1>
      <p className="page-subtitle">Index quotes from the configured finance provider.</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <button className="btn" onClick={() => void load()} disabled={loading}>
        {loading ? 'Refreshing…' : '↻ Refresh'}
      </button>

      {data && !data.available && (
        <p className="empty" style={{ marginTop: 12 }}>Market data is unavailable (provider unreachable or no indices configured).</p>
      )}

      {data?.available && indices.length > 0 && (
        <>
          <h2 style={{ marginTop: 24 }}>Indices</h2>
          <div className="table-scroll">
            <table className="data-table">
              <thead><tr><th>Symbol</th><th>Price</th><th>Change</th><th>Change %</th><th>Currency</th><th>As of</th></tr></thead>
              <tbody>
                {indices.map((q: any, i: number) => (
                  <tr key={i}>
                    <td><strong>{q.symbol}</strong></td>
                    <td>{q.price != null ? q.price.toLocaleString?.() ?? q.price : '—'}</td>
                    <td style={{ color: q.change != null ? (q.change < 0 ? 'var(--danger, #c00)' : 'var(--success, #080)') : undefined }}>
                      {q.change != null ? q.change.toFixed?.(2) ?? q.change : '—'}
                    </td>
                    <td>{q.change_percent != null ? `${q.change_percent > 0 ? '+' : ''}${q.change_percent}%` : '—'}</td>
                    <td>{q.currency ?? '—'}</td>
                    <td>{q.as_of ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <details style={{ marginTop: 24 }}>
            <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-muted)' }}>Raw response</summary>
            <JsonViewer data={data} />
          </details>
        </>
      )}
    </div>
  );
}
