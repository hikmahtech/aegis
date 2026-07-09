import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type Charge = {
  id: string;
  account: string;
  vendor_name: string;
  category: string;
  amount_cents: number;
  currency: string;
  monthly_home_equivalent: number;
  cadence: string;
  next_due_at: string | null;
  status: string;
  last_seen_at: string | null;
  first_seen_at: string | null;
};

type RenewalAlert = {
  charge_id: string;
  threshold_days: number;
  fired_at: string;
  vendor_name: string;
  amount_cents: number;
  currency: string;
  next_due_at: string | null;
};

type MoneyState = {
  charges: Charge[];
  upcoming_alerts: RenewalAlert[];
  home_currency?: string;
};

const CURRENCY_SYMBOL: Record<string, string> = {
  INR: '₹', USD: '$', EUR: '€', GBP: '£', JPY: '¥', SGD: 'S$', AUD: 'A$', CAD: 'C$',
};
function currencySymbol(code: string | undefined): string {
  if (!code) return '₹';
  return CURRENCY_SYMBOL[code] ?? `${code} `;
}

type DigestSummary = {
  total_monthly_inr?: number;
  active_count?: number;
  by_category?: Record<string, { total_inr: number; count: number }>;
  top_spenders?: Array<{ vendor_name: string; monthly_home_equivalent: number }>;
  new_this_month?: any[];
  cancelled_this_month?: any[];
};

type DigestRow = {
  period_start: string;
  period_end: string;
  summary: DigestSummary;
  sent_at: string | null;
};

function formatAmount(cents: number, currency: string): string {
  return `${(cents / 100).toFixed(2)} ${currency}`;
}

// Collapse repeat firings of the same (charge × threshold) into one row,
// keeping the most recent ``fired_at`` and a count of how many times the
// alert fired. The backend emits one row per (charge, threshold) firing, so
// the same renewal can appear 5+ times across thresholds and re-checks.
type DedupedAlert = RenewalAlert & { count: number };
function dedupAlerts(alerts: RenewalAlert[]): DedupedAlert[] {
  const byKey = new Map<string, DedupedAlert>();
  for (const a of alerts) {
    const key = `${a.charge_id}:${a.threshold_days}`;
    const prev = byKey.get(key);
    if (!prev) {
      byKey.set(key, { ...a, count: 1 });
      continue;
    }
    prev.count += 1;
    if (a.fired_at && (!prev.fired_at || a.fired_at > prev.fired_at)) {
      prev.fired_at = a.fired_at;
    }
  }
  return [...byKey.values()].sort((a, b) => {
    // Most-urgent first: smaller threshold_days then most recent fire.
    if (a.threshold_days !== b.threshold_days) return a.threshold_days - b.threshold_days;
    return (b.fired_at || '').localeCompare(a.fired_at || '');
  });
}

export default function Money() {
  const [data, setData] = useState<MoneyState | null>(null);
  const [digest, setDigest] = useState<DigestRow | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [running, setRunning] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [state, digestResp] = await Promise.all([
        api.moneyState(),
        api.moneyDigest(),
      ]);
      setData(state);
      setDigest(digestResp?.digest ?? null);
    } catch (e: any) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }

  async function recheck(flow: string) {
    setRunning(flow);
    try {
      await api.moneyRunFlow(flow);
      await load();
    } catch (e: any) {
      setError(e);
    } finally {
      setRunning(null);
    }
  }

  useEffect(() => { void load(); }, []);

  if (loading && !data) return <div className="loading">Loading money state…</div>;

  const charges = data?.charges ?? [];
  const alerts = data?.upcoming_alerts ?? [];
  const sym = currencySymbol(data?.home_currency);
  const summary = digest?.summary ?? null;
  const byCategory = summary?.by_category ?? {};
  const categoryRows = Object.entries(byCategory).sort(
    (a, b) => b[1].total_inr - a[1].total_inr,
  );

  return (
    <div>
      <h1 className="page-title">Money Hygiene</h1>
      <p className="page-subtitle">
        Recurring charges, renewal alerts, and the latest monthly subscription digest.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {/* Active recurring charges */}
      <section style={{ marginTop: 24 }}>
        <div className="filter-bar" style={{ justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>
            Active recurring charges
          </h2>
          <button
            className="btn"
            disabled={running === 'receipt_scan'}
            onClick={() => void recheck('receipt_scan')}
          >
            {running === 'receipt_scan' ? 'Running…' : '↻ Re-scan receipts'}
          </button>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Vendor</th>
                <th>Category</th>
                <th>Amount</th>
                <th>{sym}/mo</th>
                <th>Cadence</th>
                <th>Next due</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {charges.length === 0 && (
                <tr><td colSpan={7} className="empty">No recurring charges yet</td></tr>
              )}
              {charges.map((c) => (
                <tr key={c.id} style={{ opacity: c.status === 'cancelled' ? 0.5 : 1 }}>
                  <td><strong>{c.vendor_name}</strong></td>
                  <td>{c.category}</td>
                  <td className="mono">{formatAmount(c.amount_cents, c.currency)}</td>
                  <td className="mono">{sym}{Number(c.monthly_home_equivalent ?? 0).toFixed(0)}</td>
                  <td>{c.cadence}</td>
                  <td>{c.next_due_at ? new Date(c.next_due_at).toLocaleDateString() : '—'}</td>
                  <td>
                    <span className={`badge badge-${c.status === 'active' ? 'success' : c.status === 'cancelled' ? 'error' : 'info'}`}>
                      {c.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Upcoming renewal alerts — collapse repeated (charge × threshold) rows
          to the most recent firing per vendor so the page isn't 50 lines of
          near-duplicates. */}
      <section style={{ marginTop: 24 }}>
        <div className="filter-bar" style={{ justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>
            Upcoming renewal alerts
          </h2>
          <button
            className="btn"
            disabled={running === 'money_hygiene'}
            onClick={() => void recheck('money_hygiene')}
          >
            {running === 'money_hygiene' ? 'Running…' : '↻ Re-run money hygiene'}
          </button>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Vendor</th>
                <th>Threshold (days)</th>
                <th>Amount</th>
                <th>Next due</th>
                <th>Last fired</th>
                <th className="mono"># fires</th>
              </tr>
            </thead>
            <tbody>
              {alerts.length === 0 && (
                <tr><td colSpan={6} className="empty">No renewal alerts</td></tr>
              )}
              {dedupAlerts(alerts).map((a) => (
                <tr
                  key={`${a.charge_id}:${a.threshold_days}`}
                  style={{ color: a.threshold_days <= 7 ? 'var(--danger, #e53e3e)' : undefined }}
                >
                  <td><strong>{a.vendor_name}</strong></td>
                  <td className="mono">{a.threshold_days}</td>
                  <td className="mono">{formatAmount(a.amount_cents, a.currency)}</td>
                  <td>{a.next_due_at ? new Date(a.next_due_at).toLocaleDateString() : '—'}</td>
                  <td>{a.fired_at ? new Date(a.fired_at).toLocaleString() : '—'}</td>
                  <td className="mono">{a.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {/* Latest monthly digest */}
      <section style={{ marginTop: 24 }}>
        <div className="filter-bar" style={{ justifyContent: 'space-between' }}>
          <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>
            Latest monthly digest
          </h2>
          <button
            className="btn"
            disabled={running === 'subscription_audit'}
            onClick={() => void recheck('subscription_audit')}
          >
            {running === 'subscription_audit' ? 'Running…' : '↻ Re-run audit'}
          </button>
        </div>
        {!digest && (
          <div className="empty" style={{ padding: 16 }}>No digest generated yet.</div>
        )}
        {digest && summary && (
          <div style={{ marginTop: 12 }}>
            <p style={{ margin: '4px 0' }}>
              <strong>Period:</strong> {digest.period_start} → {digest.period_end}
            </p>
            <p style={{ margin: '4px 0' }}>
              <strong>Active charges:</strong> {summary.active_count ?? 0}
            </p>
            <p style={{ margin: '4px 0' }}>
              <strong>Total monthly burn:</strong> {sym}
              {Number(summary.total_monthly_inr ?? 0).toFixed(0)}
            </p>

            {categoryRows.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <h3 style={{ margin: '8px 0', fontSize: 14, fontWeight: 600 }}>By category</h3>
                <div className="table-scroll">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Category</th>
                        <th>Total {sym}/mo</th>
                        <th>Charges</th>
                      </tr>
                    </thead>
                    <tbody>
                      {categoryRows.map(([cat, info]) => (
                        <tr key={cat}>
                          <td><strong>{cat}</strong></td>
                          <td className="mono">{sym}{info.total_inr.toFixed(0)}</td>
                          <td className="mono">{info.count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {summary.top_spenders && summary.top_spenders.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <h3 style={{ margin: '8px 0', fontSize: 14, fontWeight: 600 }}>Top spenders</h3>
                <div className="table-scroll">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Vendor</th>
                        <th>{sym}/mo</th>
                      </tr>
                    </thead>
                    <tbody>
                      {summary.top_spenders.map((s, i) => (
                        <tr key={`${s.vendor_name}-${i}`}>
                          <td><strong>{s.vendor_name}</strong></td>
                          <td className="mono">{sym}{Number(s.monthly_home_equivalent ?? 0).toFixed(0)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
