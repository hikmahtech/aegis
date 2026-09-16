import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import JsonViewer from '../components/JsonViewer';
import DataTable from '../components/DataTable';

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
            <DataTable
              rows={indices as any[]}
              columns={[
                { header: 'Symbol', cell: q => <strong>{q.symbol}</strong> },
                { header: 'Price', cell: q => (q.price != null ? q.price.toLocaleString?.() ?? q.price : '—') },
                {
                  header: 'Change',
                  td: q => ({
                    style: { color: q.change != null ? (q.change < 0 ? 'var(--danger)' : 'var(--success)') : undefined },
                  }),
                  cell: q => (q.change != null ? q.change.toFixed?.(2) ?? q.change : '—'),
                },
                {
                  header: 'Change %',
                  cell: q => (q.change_percent != null ? `${q.change_percent > 0 ? '+' : ''}${q.change_percent}%` : '—'),
                },
                { header: 'Currency', cell: q => q.currency ?? '—' },
                { header: 'As of', cell: q => q.as_of ?? '—' },
              ]}
            />
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
