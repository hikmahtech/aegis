/**
 * Maou's trading desk — how it is doing, what it holds, what it did, and what
 * it is complaining about.
 *
 * Read-only, deliberately. The desk trades on paper against real prices and
 * every order on this page was placed by the daily run; a button here that
 * could buy, sell or change the rules would be a second way to move money with
 * none of the run's checks in front of it.
 *
 * Every figure comes from the desk's own code: the positions are
 * `desk_math.replay` over the filled orders, the score is
 * `trading_desk.month_summary` — the identical call the monthly close makes —
 * the return chart is `desk_view.series`, built from the same calls, and the
 * complaints are the problem hub's rows for this subject kind.
 */

import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import DeskRulesPanel from '../components/DeskRulesPanel';
import DataTable from '../components/DataTable';
import { LineChart, Legend, StackBar, type Series } from '../components/charts';
import {
  fmtAmount,
  fmtPct,
  fmtSignedPct,
  moneyApi,
  type DeskFinding,
  type DeskHistory,
  type DeskSeries,
  type DeskState,
} from '../lib/moneyApi';

// Money columns: right-aligned and monospaced, the same pair on every table here.
const RIGHT_MONO = {
  th: { style: { textAlign: 'right' as const } },
  td: { className: 'mono', style: { textAlign: 'right' as const } },
};

/** What the desk decided on a day, said the way a person would say it. */
const OUTCOME: Record<string, { label: string; badge: string }> = {
  orders: { label: 'placed orders', badge: 'success' },
  no_change: { label: 'nothing to change', badge: 'neutral' },
  held_stale: { label: 'held — no decisions arrived', badge: 'pending' },
  held_suspect: { label: 'held — the decisions looked wrong', badge: 'error' },
  flattened: { label: 'sold everything — risk halt', badge: 'error' },
};

const STATUS_BADGE: Record<string, string> = {
  filled: 'success',
  pending: 'pending',
  cancelled: 'neutral',
};

// The palette the charts and the allocation bar share, all tokens.
const PALETTE = [
  'var(--accent)', 'var(--success)', 'var(--orange)', 'var(--info)',
  'var(--purple)', 'var(--warning)', 'var(--danger)',
];

const RANGES: Array<[string, number]> = [['1M', 22], ['3M', 66], ['6M', 132], ['All', 0]];

type Tab = 'performance' | 'holdings' | 'activity' | 'complaints' | 'settings';

/** The sentence the desk wrote when it raised a finding, or its title. */
function findingText(f: DeskFinding): string {
  return f.payload?.description || f.title || `${f.klass ?? ''} ${f.subject ?? ''}`.trim();
}

/** "2026-09-24" → "24 Sep". */
function shortDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

/** Each value's fall from the highest value before it (0 at a new high). */
function drawdowns(values: number[]): number[] {
  let peak = 0;
  return values.map(v => {
    peak = Math.max(peak, v);
    return peak ? v / peak - 1 : 0;
  });
}

function signClass(v: number | null | undefined): string {
  return (v ?? 0) >= 0 ? 'up' : 'down';
}

/**
 * The return chart, the drawdown and the invested share, over a chosen window.
 * "All" measures from capital, so the first day's costs show as the small dip
 * they are; a shorter window measures from its own first close.
 */
function Performance({ series }: { series: DeskSeries }) {
  const [range, setRange] = useState(0);
  const days = useMemo(
    () => (range ? series.days.slice(-range - 1) : series.days),
    [series.days, range],
  );
  if (!series.days.length) {
    return (
      <div className="card">
        <h3>No line to draw yet</h3>
        <p>The chart starts at the desk&rsquo;s first fill.</p>
      </div>
    );
  }
  const base = (pick: (d: DeskSeries['days'][number]) => number | null) => {
    const first = range ? pick(days[0]) : series.capital;
    return days.map(d => {
      const v = pick(d);
      return v === null || !first ? null : v / first - 1;
    });
  };
  const lines: Series[] = [
    { name: 'The desk', color: 'var(--accent)', values: base(d => d.value) },
  ];
  if (series.benchmark) {
    lines.push({ name: series.benchmark, color: 'var(--orange)', values: base(d => d.benchmark) });
  }
  if (series.context) {
    lines.push({
      name: series.context, color: 'var(--text-subtle)', dashed: true, values: base(d => d.context),
    });
  }
  const drawdown = drawdowns(days.map(d => d.value));
  const worst = Math.min(...drawdown);
  const labels = days.map(d => shortDay(d.day));
  const last = (s: Series) => s.values[s.values.length - 1];

  return (
    <>
      <div className="chart-card">
        <div className="section-header-row">
          <div>
            <h3>Return</h3>
            <span className="meta">
              {range ? 'from the first close in the window' : 'since the first fill, on the whole capital'}
              {' · '}the benchmarks are the same money bought and held
            </span>
          </div>
          <span className="range-chips">
            {RANGES.map(([label, n]) => (
              <button key={label} className={`btn ${range === n ? 'active' : ''}`} onClick={() => setRange(n)}>
                {label}
              </button>
            ))}
          </span>
        </div>
        <div className="stats-bar" style={{ margin: '0.6rem 0 0' }}>
          {lines.map(s => (
            <div className="stat-item" key={s.name}>
              <span className={`stat-value ${signClass(last(s))}`}>{fmtSignedPct(last(s), 2)}</span>
              <span className="stat-label">{s.name}</span>
            </div>
          ))}
        </div>
        <Legend series={lines} />
        <LineChart labels={labels} series={lines} fmt={v => fmtSignedPct(v, 1)} zero height={260} />
      </div>
      <div className="chart-row">
        <div className="chart-card">
          <h3>Drawdown</h3>
          <span className="meta">
            How far below its best close the desk stood. Worst in this window:{' '}
            <strong className="down">{fmtSignedPct(worst, 2)}</strong>.
          </span>
          <LineChart
            labels={labels}
            series={[{ name: 'Below the peak', color: 'var(--danger)', values: drawdown, area: true }]}
            fmt={v => fmtPct(v, 1)}
            zero
            height={180}
          />
        </div>
        <div className="chart-card">
          <h3>How much was invested</h3>
          <span className="meta">
            The share of the desk in the market at each close; the rest was cash.
          </span>
          <LineChart
            labels={labels}
            series={[{
              name: 'Invested', color: 'var(--info)', area: true,
              values: days.map(d => d.invested_pct),
            }]}
            fmt={v => fmtPct(v, 0)}
            zero
            height={180}
          />
        </div>
      </div>
    </>
  );
}

/**
 * The score's headline, in words. `label` is the desk's own verdict on how much
 * weight to give the number ("too early", "no evidence yet", "strong"), so it
 * is printed rather than re-interpreted here.
 */
function ScoreCard({ desk }: { desk: DeskState }) {
  const s = desk.score;
  // Every figure is printed in the desk's own currency, which travels with the
  // figures rather than being assumed by the page.
  const money = (v: number | null | undefined) => fmtAmount(v, desk.currency);
  if (!s) {
    return (
      <div className="card">
        <h3>No score yet</h3>
        <p>
          The desk scores itself from its first fill onwards. Nothing has filled, so there is
          nothing to compare against {desk.benchmark} yet.
        </p>
      </div>
    );
  }
  return (
    <>
      <div className="grid">
        <div className="card">
          <h3>The weekly gap</h3>
          <p>
            Week by week the desk beat {s.benchmark} by an average of{' '}
            <strong>{fmtSignedPct(s.mean_gap)}</strong> over <strong>{s.weeks}</strong>{' '}
            {s.weeks === 1 ? 'week' : 'weeks'} (t = {s.t.toFixed(1)}).
          </p>
          <p style={{ marginTop: '0.5rem' }}>
            How much to read into that: <strong>{s.label}</strong>.
          </p>
        </div>
        <div className="card">
          <h3>The monthly check</h3>
          {s.below_expectation ? (
            <p style={{ color: 'var(--danger-text)' }}>
              <strong>Firing.</strong> Live results are more than two standard errors below the{' '}
              {fmtPct(s.expected_excess_pa, 0)} a year the backtest implies. It comes back every
              month while that stays true — look at the trading system before trusting it with
              more money.
            </p>
          ) : (
            <p>
              Quiet. The check fires when live results fall more than two standard errors below
              the {fmtPct(s.expected_excess_pa, 0)} a year the backtest implies.
            </p>
          )}
        </div>
        <div className="card">
          <h3>This month</h3>
          <div className="meta-row"><span>Orders filled</span><span>{s.filled}</span></div>
          <div className="meta-row"><span>Costs paid</span><span>{money(s.costs)}</span></div>
          <div className="meta-row">
            <span>Days it held back</span>
            <span>{Object.values(s.held_back).reduce((a, b) => a + b, 0) || 0}</span>
          </div>
          <div className="meta-row">
            <span>Orders cancelled</span>
            <span>{Object.values(s.cancelled).reduce((a, b) => a + b, 0) || 0}</span>
          </div>
          <div className="meta-row">
            <span>After tax if sold</span>
            <span>{desk.taxed ? money(s.after_tax) : 'no tax model set'}</span>
          </div>
          <div className="meta-row"><span>Scoring since</span><span>{s.since}</span></div>
        </div>
      </div>
      {!!s.moves.length && (
        <section className="section" style={{ marginTop: '1.5rem' }}>
          <h2 className="section-title">Prices worth a second look</h2>
          <p className="meta" style={{ marginBottom: 10 }}>
            A close that moved more than half in a day with no split recorded. Usually a bad
            price rather than a real move.
          </p>
          <div className="table-scroll">
            <DataTable
              rows={s.moves}
              rowKey={m => `${m.symbol}-${m.day}`}
              columns={[
                { header: 'Symbol', td: { className: 'mono' }, cell: m => m.symbol },
                { header: 'Day', td: { className: 'mono' }, cell: m => m.day },
                { header: 'Move', ...RIGHT_MONO, cell: m => fmtSignedPct(m.move, 1) },
              ]}
            />
          </div>
        </section>
      )}
    </>
  );
}

function Holdings({ desk }: { desk: DeskState }) {
  const money = (v: number | null | undefined) => fmtAmount(v, desk.currency);
  const parts = desk.positions.map((p, i) => ({
    label: p.symbol, share: p.weight ?? 0, color: PALETTE[i % PALETTE.length],
  }));
  parts.push({ label: 'Cash', share: desk.cash_pct ?? 0, color: 'var(--border-strong)' });
  return (
    <>
      <div className="chart-card">
        <div className="section-header-row">
          <h3>Where the money sits</h3>
          <span className="meta">as of {desk.as_of}</span>
        </div>
        <StackBar parts={parts} />
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th style={{ textAlign: 'right' }}>Shares</th>
              <th style={{ textAlign: 'right' }}>Paid each</th>
              <th style={{ textAlign: 'right' }}>Last price</th>
              <th style={{ textAlign: 'right' }}>Worth now</th>
              <th style={{ textAlign: 'right' }}>Up or down</th>
              <th style={{ textAlign: 'right' }}>Share</th>
            </tr>
          </thead>
          <tbody>
            {!desk.positions.length && (
              <tr>
                <td colSpan={7} className="empty">Nothing held yet — it is all still in cash.</td>
              </tr>
            )}
            {desk.positions.map((p, i) => (
              <tr key={p.symbol}>
                <td>
                  <i className="dot" style={{
                    display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
                    background: PALETTE[i % PALETTE.length], marginRight: 8,
                  }} />
                  <strong>{p.symbol}</strong>
                  <span className="meta" style={{ marginLeft: 6 }}>{p.asset_class}</span>
                  {!p.priced && (
                    <span className="badge badge-error" style={{ marginLeft: 6 }}>
                      no price — held at cost
                    </span>
                  )}
                </td>
                <td className="mono" style={{ textAlign: 'right' }}>{p.qty}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{money(p.avg_cost)}</td>
                <td className="mono" style={{ textAlign: 'right' }}>
                  {money(p.last_close)}
                  {p.priced_on && p.priced_on !== desk.as_of && (
                    <div className="meta" style={{ fontSize: 11 }}>on {p.priced_on}</div>
                  )}
                </td>
                <td className="mono" style={{ textAlign: 'right' }}>{money(p.value)}</td>
                <td className={`mono ${signClass(p.gain)}`} style={{ textAlign: 'right' }}>
                  {money(p.gain)}
                  <div style={{ fontSize: 11 }}>
                    {p.cost ? fmtSignedPct((p.gain ?? 0) / p.cost, 1) : ''}
                  </div>
                </td>
                <td className="mono" style={{ textAlign: 'right' }}>{fmtPct(p.weight)}</td>
              </tr>
            ))}
            <tr>
              <td><strong>Cash</strong></td>
              <td colSpan={3} />
              <td className="mono" style={{ textAlign: 'right' }}>{money(desk.cash)}</td>
              <td />
              <td className="mono" style={{ textAlign: 'right' }}>{fmtPct(desk.cash_pct)}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}

function Activity({ desk, history }: { desk: DeskState; history: DeskHistory | null }) {
  const money = (v: number | null | undefined) => fmtAmount(v, desk.currency);
  return (
    <>
      {!!desk.pending.length && (
        <section className="section">
          <h2 className="section-title">Waiting to fill</h2>
          <p className="meta" style={{ marginBottom: 10 }}>
            Orders placed and not yet filled. Each fills on the next session it can price; the
            money for a buy is already set aside, so today&rsquo;s sizing cannot spend it twice.
          </p>
          <div className="table-scroll">
            <DataTable
              rows={desk.pending}
              rowKey={o => o.id}
              columns={[
                { header: 'Placed', td: { className: 'mono', style: { whiteSpace: 'nowrap' } }, cell: o => o.created_day },
                { header: 'Symbol', cell: o => <strong>{o.symbol}</strong> },
                {
                  header: 'Side',
                  cell: o => <span className={`badge badge-${o.side === 'buy' ? 'info' : 'pending'}`}>{o.side}</span>,
                },
                { header: 'Shares', ...RIGHT_MONO, cell: o => o.qty },
                { header: 'Priced at', ...RIGHT_MONO, cell: o => money(o.ref_price) },
                { header: 'About', ...RIGHT_MONO, cell: o => money(o.est_value) },
              ]}
            />
          </div>
        </section>
      )}

      <section className="section">
        <h2 className="section-title">Day by day</h2>
        <p className="meta" style={{ marginBottom: 12 }}>
          One row per decision date, whatever the desk decided. Open a day to see its orders.
          The reasons are the day&rsquo;s own copy: a later rerun cannot rewrite them.
        </p>
        {!history?.days.length && <div className="empty">The desk has not run yet.</div>}
        <div className="card-vertical-list">
          {(history?.days ?? []).map(day => {
            const o = OUTCOME[day.outcome] ?? { label: day.outcome, badge: 'neutral' };
            const buys = day.orders.filter(x => x.side === 'buy').length;
            const sells = day.orders.length - buys;
            const worth = (side: string) => day.orders
              .filter(x => x.side === side)
              .reduce((n, x) => n + x.qty * (x.fill_price ?? x.ref_price ?? 0), 0);
            return (
              <details className="card" key={day.data_date} style={{ padding: '0.6rem 0.9rem' }}>
                <summary style={{ cursor: 'pointer', display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
                  <strong className="mono">{day.data_date}</strong>
                  <span className={`badge badge-${o.badge}`}>{o.label}</span>
                  {!!day.orders.length && (
                    <span className="meta">
                      {[
                        buys && `bought ${money(worth('buy'))} in ${buys}`,
                        sells && `sold ${money(worth('sell'))} in ${sells}`,
                      ].filter(Boolean).join(' · ')}
                    </span>
                  )}
                  {!!day.findings.length && <span className="badge badge-pending">{day.findings.length} finding{day.findings.length > 1 ? 's' : ''}</span>}
                </summary>
                <div style={{ marginTop: '0.6rem' }}>
                  {day.note && <p className="meta">{day.note}</p>}
                  {day.findings.map((f, i) => (
                    <p key={i} style={{ color: 'var(--warning-text)' }}>{findingText(f)}</p>
                  ))}
                  {!!day.skipped.length && (
                    <p className="meta">Left alone because an order was still open: {day.skipped.join(', ')}.</p>
                  )}
                  {!!day.orders.length && (
                    <div className="table-scroll">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th>Symbol</th>
                            <th>Side</th>
                            <th style={{ textAlign: 'right' }}>Shares</th>
                            <th style={{ textAlign: 'right' }}>Sized at</th>
                            <th>Outcome</th>
                            <th style={{ textAlign: 'right' }}>Filled at</th>
                            <th style={{ textAlign: 'right' }}>Costs</th>
                          </tr>
                        </thead>
                        <tbody>
                          {day.orders.map(ord => (
                            <tr key={`${day.data_date}-${ord.seq}`}>
                              <td><strong>{ord.symbol}</strong></td>
                              <td>{ord.side}</td>
                              <td className="mono" style={{ textAlign: 'right' }}>{ord.qty}</td>
                              <td className="mono" style={{ textAlign: 'right' }}>{money(ord.ref_price)}</td>
                              <td>
                                <span className={`badge badge-${STATUS_BADGE[ord.status] ?? 'neutral'}`}>{ord.status}</span>
                                {ord.reason && <div className="meta" style={{ fontSize: 11 }}>{ord.reason}</div>}
                              </td>
                              <td className="mono" style={{ textAlign: 'right' }}>
                                {money(ord.fill_price)}
                                {ord.fill_date && (
                                  <div className="meta" style={{ fontSize: 11 }}>
                                    {ord.fill_date}
                                    {ord.price_kind ? ` · ${ord.price_kind}` : ''}
                                    {ord.price_source === 'ansaar' ? ' · ansaar price' : ''}
                                  </div>
                                )}
                              </td>
                              <td className="mono" style={{ textAlign: 'right' }}>{money(ord.costs)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </details>
            );
          })}
        </div>
      </section>
    </>
  );
}

function Complaints({ desk }: { desk: DeskState }) {
  if (!desk.problems.length) return <div className="empty">Nothing open. The desk has no complaints.</div>;
  return (
    <div className="card-vertical-list">
      {desk.problems.map(p => (
        <div className="card" key={p.id}>
          <div className="section-header-row">
            <h3 style={{ marginBottom: 0 }}>{p.title}</h3>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <span className={`badge badge-${p.severity === 'critical' ? 'error' : 'pending'}`}>{p.severity}</span>
              <span className="badge badge-neutral">{p.status}</span>
              {p.muted_until && <span className="badge badge-neutral">muted</span>}
            </div>
          </div>
          {p.description && <p>{p.description}</p>}
          <p className="meta" style={{ marginTop: '0.5rem' }}>
            {p.class} · {p.subject} · seen {p.occurrences} {p.occurrences === 1 ? 'time' : 'times'}, last{' '}
            {p.last_seen_at.slice(0, 16).replace('T', ' ')}
            {p.todoist_task_id && ' · has a Todoist task'}
          </p>
        </div>
      ))}
    </div>
  );
}

function Banner({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{
      background: 'var(--warning-tint)', border: '1px solid var(--warning-text)',
      color: 'var(--warning-text)', padding: '10px 14px', margin: '0 0 1rem',
      borderRadius: 'var(--radius-sm)',
    }}>
      <strong>{title}</strong>
      <p style={{ margin: '0.4rem 0 0', fontSize: '0.9rem' }}>{children}</p>
    </div>
  );
}

export default function TradingDesk() {
  const [desk, setDesk] = useState<DeskState | null>(null);
  const [history, setHistory] = useState<DeskHistory | null>(null);
  const [series, setSeries] = useState<DeskSeries | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');
  const [params, setParams] = useSearchParams();
  const tab = (params.get('tab') as Tab) || 'performance';

  useEffect(() => {
    let live = true;
    void Promise.allSettled([moneyApi.desk(), moneyApi.deskHistory(), moneyApi.deskSeries()])
      .then(([d, h, s]) => {
        if (!live) return;
        if (d.status === 'fulfilled') setDesk(d.value);
        else setError((d.reason as Error)?.message || 'Could not read the desk');
        if (h.status === 'fulfilled') setHistory(h.value);
        if (s.status === 'fulfilled') setSeries(s.value);
        setLoading(false);
      });
    return () => { live = false; };
  }, []);

  if (loading) return <div className="loading">Reading the desk…</div>;
  if (!desk) return <div className="error">{error || 'Could not read the desk'}</div>;

  const gainPct = desk.gain !== null && desk.capital ? desk.gain / desk.capital : null;
  const money = (v: number | null | undefined) => fmtAmount(v, desk.currency);
  const lastDay = series?.days[series.days.length - 1];
  const benchPct = lastDay?.benchmark && series ? lastDay.benchmark / series.capital - 1 : null;
  const tabs: Array<[Tab, string, number?]> = [
    ['performance', 'Performance'],
    ['holdings', 'Holdings', desk.positions.length],
    ['activity', 'Activity', desk.pending.length || undefined],
    ['complaints', 'Complaints', desk.problems.length || undefined],
    ['settings', 'Settings'],
  ];

  return (
    <div>
      <h1 className="page-title">Trading desk</h1>
      <p className="page-subtitle">
        Maou trades the trading system&rsquo;s picks on paper out of {money(desk.capital)}
        {desk.benchmark ? <>, scored against {desk.benchmark}</> : ''}. The daily run places
        every order; nothing on this page can trade.
      </p>

      {error && <div className="error">{error}</div>}
      {!desk.configured && (
        <Banner title="No market is set, so the desk is doing nothing.">
          It needs a trading calendar before it can tell a market day from a holiday. Set one
          under Settings.
        </Banner>
      )}
      {desk.mode !== 'paper' && (
        <Banner title={`Mode is "${desk.mode}", not paper.`}>
          Only paper mode is built. The desk trades nothing until this is set back in the{' '}
          <code>trading-desk-daily</code> config.
        </Banner>
      )}

      <div className="stats-bar">
        <div className="stat-item">
          <span className="stat-value">{money(desk.value)}</span>
          <span className="stat-label">Worth today · as of {desk.as_of}</span>
        </div>
        <div className="stat-item">
          <span className={`stat-value ${signClass(desk.gain)}`}>
            {fmtSignedPct(gainPct, 2)}
          </span>
          <span className="stat-label">Return · {money(desk.gain)}</span>
        </div>
        {desk.benchmark && (
          <div className="stat-item">
            <span className={`stat-value ${signClass(benchPct)}`}>{fmtSignedPct(benchPct, 2)}</span>
            <span className="stat-label">{desk.benchmark} · same money</span>
          </div>
        )}
        <div className="stat-item">
          <span className="stat-value">{fmtPct(desk.value ? (desk.invested ?? 0) / desk.value : null, 0)}</span>
          <span className="stat-label">Invested · {money(desk.cash)} in cash</span>
        </div>
        <div className="stat-item">
          <span className={`stat-value ${desk.problems.length ? 'down' : ''}`}>{desk.problems.length}</span>
          <span className="stat-label">Open complaints</span>
        </div>
      </div>

      <div className="tabs" role="tablist">
        {tabs.map(([key, label, n]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? 'active' : ''}
            onClick={() => setParams(key === 'performance' ? {} : { tab: key }, { replace: true })}
          >
            {label}
            {n !== undefined && (
              <span className={`badge badge-${key === 'complaints' ? 'error' : 'neutral'}`}>{n}</span>
            )}
          </button>
        ))}
      </div>

      {tab === 'performance' && (
        <>
          {series ? <Performance series={series} /> : <div className="empty">Could not read the return series.</div>}
          <section className="section" style={{ marginTop: '1.75rem' }}>
            <h2 className="section-title">The score</h2>
            <p className="meta" style={{ marginBottom: 12 }}>
              The same figures the monthly close reports.
            </p>
            <ScoreCard desk={desk} />
          </section>
        </>
      )}
      {tab === 'holdings' && <Holdings desk={desk} />}
      {tab === 'activity' && <Activity desk={desk} history={history} />}
      {tab === 'complaints' && <Complaints desk={desk} />}
      {tab === 'settings' && (
        <DeskRulesPanel onSaved={() => void moneyApi.desk().then(setDesk, () => {})} />
      )}
    </div>
  );
}
