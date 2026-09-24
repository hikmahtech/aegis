import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import ErrorBanner from '../components/ErrorBanner';
import ChartPanel from '../components/ChartPanel';
import MoneyAccounting from '../components/MoneyAccounting';
import { BarChart, HBars, Legend, LineChart } from '../components/charts';
import { fmtMoney } from '../lib/money';
import { moneyApi, type MoneyDigest, type MoneyState, type MoneyTrend } from '../lib/moneyApi';

const KIND_BADGE: Record<string, string> = {
  transaction: 'success',
  due: 'pending',
  failed: 'error',
};

type Tab = 'overview' | 'bills' | 'unexplained' | 'statements' | 'events' | 'close' | 'setup';

const TABS: Array<[Tab, string]> = [
  ['overview', 'Overview'],
  ['bills', 'Bills'],
  ['unexplained', 'Unexplained'],
  ['statements', 'Statements'],
  ['events', 'Recent events'],
  ['close', 'Month close'],
  ['setup', 'Setup'],
];

/** A Todoist task link, but only for a real task id — an `item-…` ref is a
 *  temp id still sitting in the outbox and Todoist has never seen it. */
function todoistHref(ref: string): string | null {
  return /^\d+$/.test(ref) ? `https://app.todoist.com/app/task/${ref}` : null;
}

/** hledger's "2026-07" → "Jul 26"; anything else is printed as it came. */
function monthLabel(period: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(period);
  if (!m) return period;
  return new Date(+m[1], +m[2] - 1, 1).toLocaleDateString(undefined, { month: 'short', year: '2-digit' });
}

/** A chart figure: the home symbol and a short Indian-grouped number (₹7.04L).
 *  The tables print hledger's exact cells; this is only for reading a shape. */
function compact(symbol: string) {
  const f = new Intl.NumberFormat('en-IN', { notation: 'compact', maximumFractionDigits: 1 });
  return (v: number) => `${v < 0 ? '-' : ''}${symbol}${f.format(Math.abs(v))}`;
}

function Trend({ trend }: { trend: MoneyTrend }) {
  if (!trend.books_ok) {
    return <div className="error">Could not read the books for the charts: {trend.error}</div>;
  }
  const months = trend.months;
  const fmt = compact(trend.home_symbol);
  const exact = (v: number) => fmtMoney(v.toFixed(2), trend.home_currency);
  const now = months[months.length - 1];
  const before = months[months.length - 2];
  const labels = months.map(m => monthLabel(m.month));
  const flows = [
    { name: 'Money in', color: 'var(--success)', values: months.map(m => m.income) },
    { name: 'Money out', color: 'var(--danger)', values: months.map(m => m.expenses) },
  ];
  const saved = now && now.income ? now.net / now.income : null;

  return (
    <>
      {now && (
        <div className="stats-bar">
          <div className="stat-item">
            <span className="stat-value">{exact(now.net_worth)}</span>
            <span className="stat-label">
              Net worth
              {before ? ` · ${now.net_worth >= before.net_worth ? '+' : ''}${fmt(now.net_worth - before.net_worth)} on last month` : ''}
            </span>
          </div>
          <div className="stat-item">
            <span className="stat-value up">{exact(now.income)}</span>
            <span className="stat-label">In this month</span>
          </div>
          <div className="stat-item">
            <span className="stat-value down">{exact(now.expenses)}</span>
            <span className="stat-label">Out this month</span>
          </div>
          <div className="stat-item">
            <span className={`stat-value ${now.net >= 0 ? 'up' : 'down'}`}>
              {saved === null ? '—' : `${(saved * 100).toFixed(0)}%`}
            </span>
            <span className="stat-label">Kept of what came in · {exact(now.net)}</span>
          </div>
        </div>
      )}

      <div className="chart-row" style={{ marginBottom: '1rem' }}>
        <div className="chart-card">
          <h3>Net worth</h3>
          <span className="meta">Everything you have less everything you owe, at each month end.</span>
          <LineChart
            labels={labels}
            series={[{ name: 'Net worth', color: 'var(--accent)', values: months.map(m => m.net_worth), area: true }]}
            fmt={fmt}
            height={200}
          />
        </div>
        <div className="chart-card">
          <h3>Money in and out</h3>
          <span className="meta">Income and spending per month. The current month is still running.</span>
          <Legend series={flows} />
          <BarChart labels={labels} series={flows} fmt={fmt} height={184} />
        </div>
      </div>

      <div className="chart-card">
        <h3>Where it went this month</h3>
        <span className="meta" style={{ display: 'block', marginBottom: 10 }}>
          Spending by category, biggest first.
        </span>
        <HBars
          rows={trend.spend.map(s => ({
            label: s.account.replace(/^expenses:/, ''),
            value: s.amount,
            note: now?.expenses ? `${((s.amount / now.expenses) * 100).toFixed(0)}%` : undefined,
          }))}
          fmt={exact}
          color="var(--danger)"
        />
      </div>

      {!!trend.unconverted.length && (
        <p className="meta" style={{ margin: '-0.4rem 0 1rem' }}>
          Left out of the charts because <code>prices.journal</code> has no rate for them:{' '}
          {trend.unconverted.join(' · ')}.
        </p>
      )}
    </>
  );
}

export default function Money() {
  const [data, setData] = useState<MoneyState | null>(null);
  const [digest, setDigest] = useState<MoneyDigest | null>(null);
  const [trend, setTrend] = useState<MoneyTrend | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const tab = (params.get('tab') as Tab) || 'overview';

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [state, digestResp, trendResp] = await Promise.all([
        moneyApi.state(),
        moneyApi.digest(),
        // The charts are extra: a books checkout that cannot answer must not
        // take the events and the month close down with it.
        moneyApi.trend().catch(() => null),
      ]);
      setData(state);
      setDigest(digestResp?.digest ?? null);
      setTrend(trendResp);
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }

  async function recheck(flow: string) {
    setRunning(flow);
    try {
      await moneyApi.runFlow(flow);
      await load();
    } catch (e) {
      setError(e as Error);
    } finally {
      setRunning(null);
    }
  }

  useEffect(() => { void load(); }, []);

  if (loading && !data) return <div className="loading">Loading the books…</div>;

  const events = data?.events ?? [];
  const runs: Array<[string, string]> = [
    ['money_brief', 'Run weekly brief'],
    ['month_close', 'Run month close'],
    ['receipt_scan', 'Re-scan receipts'],
  ];

  return (
    <div>
      <h1 className="page-title">Money</h1>
      <p className="page-subtitle">
        Where the money stands and where it went. The hledger journal is the record; everything
        here is read from it or from its index, and where the two disagree the journal wins.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {data && !data.books_configured && (
        <div
          style={{
            background: 'var(--warning-tint)',
            border: '1px solid var(--warning-text)',
            color: 'var(--warning-text)',
            padding: '10px 14px',
            margin: '0 0 1rem',
            borderRadius: 'var(--radius-sm)',
          }}
        >
          <strong>No books repo configured</strong>
          <p style={{ margin: '0.4rem 0 0', fontSize: '0.9rem' }}>
            Events are indexed but never posted to a journal, so every amount is the
            index&rsquo;s own copy with nothing to check it against. Set{' '}
            <code>books_repo_url</code> and the weekly sweep replays the backlog.
          </p>
        </div>
      )}

      <div className="tabs" role="tablist">
        {TABS.map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? 'active' : ''}
            onClick={() => setParams(key === 'overview' ? {} : { tab: key }, { replace: true })}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'overview' && trend && <Trend trend={trend} />}

      {/* Mounted on every tab so its four reads happen once; it renders only
          the tabs it owns. */}
      <MoneyAccounting tab={tab === 'overview' ? 'standing' : tab} />

      {tab === 'events' && (
        <section className="section">
          <div className="section-header-row">
            <h2 className="section-title" style={{ marginBottom: 0 }}>Recent events</h2>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {runs.map(([flow, label]) => (
                <button
                  key={flow}
                  className="btn"
                  disabled={running !== null}
                  onClick={() => void recheck(flow)}
                >
                  {running === flow ? 'Running…' : label}
                </button>
              ))}
            </div>
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Entity</th>
                  <th>Kind</th>
                  <th>Payee</th>
                  <th>Amount</th>
                  <th>Account</th>
                  <th>Channel</th>
                  <th>Parser</th>
                  <th>Links</th>
                </tr>
              </thead>
              <tbody>
                {events.length === 0 && (
                  <tr><td colSpan={9} className="empty">Nothing indexed yet</td></tr>
                )}
                {events.map((e) => {
                  const href = e.todoist_ref ? todoistHref(e.todoist_ref) : null;
                  return (
                    <tr key={e.message_id}>
                      <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                        {e.occurred_on ?? (e.due_on ? `due ${e.due_on}` : '—')}
                      </td>
                      <td>{e.entity}</td>
                      <td>
                        <span className={`badge badge-${KIND_BADGE[e.kind] ?? 'neutral'}`}>
                          {e.kind}
                        </span>
                      </td>
                      <td><strong>{e.payee || '—'}</strong></td>
                      <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                        {fmtMoney(e.amount, e.currency) || '—'}
                        {e.direction && (
                          <span className="meta" style={{ marginLeft: 6 }}>{e.direction}</span>
                        )}
                      </td>
                      <td className="mono">{e.account || '—'}</td>
                      <td>{e.channel || '—'}</td>
                      <td className="mono" title={e.confidence != null ? `confidence ${e.confidence}` : undefined}>
                        {e.parser || '—'}
                      </td>
                      <td className="mono" style={{ fontSize: 11 }}>
                        {/* The journal file is where the real posting lives: it is
                            the answer to "is this amount right?", so it is named
                            on every row that has one. */}
                        {e.journal_file
                          ? <div title="the journal file holding this posting">{e.journal_file}</div>
                          : <div className="meta">not posted</div>}
                        {e.todoist_ref && (
                          <div>
                            {href
                              ? <a href={href} target="_blank" rel="noreferrer">task</a>
                              : <span className="meta">task queued</span>}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {tab === 'close' && (
        <section className="section">
          <h2 className="section-title">Latest month close</h2>
          {!digest && (
            <div className="empty" style={{ padding: 16 }}>
              No month close filed yet — run the month close, or wait for the 1st.
            </div>
          )}
          {digest && (
            <div className="card">
              <p className="meta"><span className="mono">{digest.path}</span></p>
              <pre style={{
                whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12.5, lineHeight: 1.55,
                margin: 0, fontFamily: 'var(--mono)',
              }}>{digest.markdown}</pre>
            </div>
          )}
        </section>
      )}

      {tab === 'setup' && (
        <>
          <div className="stats-bar">
            <div className="stat-item">
              <span className="stat-value">{data?.books_configured ? 'Yes' : 'No'}</span>
              <span className="stat-label">Books configured</span>
            </div>
            <div className="stat-item">
              <span className={`stat-value ${data?.unpushed_commits ? 'down' : ''}`}>
                {data?.unpushed_commits ?? '—'}
              </span>
              <span className="stat-label">Unpushed commits</span>
            </div>
          </div>
          <ChartPanel />
        </>
      )}
    </div>
  );
}
