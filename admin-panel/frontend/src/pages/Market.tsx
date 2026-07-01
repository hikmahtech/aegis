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

  const regimeCards = ['equity_regime', 'bond_regime', 'commodity_regime', 'crypto_regime']
    .filter(k => data && data[k]);

  return (
    <div>
      <h1 className="page-title">Market</h1>
      <p className="page-subtitle">Regimes, trades, forecasts.</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <button className="btn" onClick={() => void load()} disabled={loading}>
        {loading ? 'Refreshing…' : '↻ Refresh'}
      </button>

      {data && !data.available && (
        <p className="empty" style={{ marginTop: 12 }}>Market data service is offline.</p>
      )}

      {data?.available && (
        <>
          {regimeCards.length > 0 && (
            <div className="grid" style={{ marginTop: 12 }}>
              {regimeCards.map(k => {
                const r = data[k];
                return (
                  <div key={k} className="card">
                    <h3>{k.replace(/_/g, ' ')}</h3>
                    <p><strong>{r.regime || r.regime_label || '—'}</strong></p>
                    <p style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                      {r.date && new Date(r.date).toLocaleDateString()}
                      {r.close != null && <> · close {r.close}</>}
                    </p>
                  </div>
                );
              })}
            </div>
          )}

          {Array.isArray(data.top_trades) && data.top_trades.length > 0 && (
            <>
              <h2 style={{ marginTop: 24 }}>Top trades</h2>
              <div className="table-scroll">
                <table className="data-table">
                  <thead><tr><th>Symbol</th><th>Signal</th><th>Confidence</th><th>Entry</th><th>Stop</th><th>Target</th></tr></thead>
                  <tbody>
                    {data.top_trades.map((t: any, i: number) => (
                      <tr key={i}>
                        <td><strong>{t.symbol}</strong></td>
                        <td>{t.signal || t.combined_forecast != null ? (t.combined_forecast > 0 ? 'BUY' : 'SELL') : '—'}</td>
                        <td>{t.confidence?.toFixed?.(2) ?? '—'}</td>
                        <td>{t.entry ?? t.close_price ?? '—'}</td>
                        <td>{t.stop ?? '—'}</td>
                        <td>{t.target ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {Array.isArray(data.forecasts) && data.forecasts.length > 0 && (
            <>
              <h2 style={{ marginTop: 24 }}>Forecasts</h2>
              <div className="table-scroll">
                <table className="data-table">
                  <thead><tr><th>Symbol</th><th>Window</th><th>Return</th><th>Confidence</th></tr></thead>
                  <tbody>
                    {data.forecasts.map((f: any, i: number) => (
                      <tr key={i}>
                        <td><strong>{f.symbol}</strong></td>
                        <td>{f.window || f.horizon || '—'}</td>
                        <td>{f.return != null ? `${(f.return * 100).toFixed(2)}%` : f.combined_forecast != null ? `${f.combined_forecast.toFixed(2)}%` : '—'}</td>
                        <td>{f.confidence?.toFixed?.(2) ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          <details style={{ marginTop: 24 }}>
            <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-muted)' }}>Raw response</summary>
            <JsonViewer data={data} />
          </details>
        </>
      )}
    </div>
  );
}
