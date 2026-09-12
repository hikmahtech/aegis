/**
 * Maou's trading desk — what it holds, how it is doing, what it did, and what
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
 * and the complaints are the problem hub's rows for this subject kind.
 */

import { useEffect, useState } from 'react';
import {
  fmtAmount,
  fmtPct,
  fmtSignedPct,
  moneyApi,
  type DeskFinding,
  type DeskHistory,
  type DeskState,
} from '../lib/moneyApi';

/** What the desk decided on a day, said the way a person would say it. */
const OUTCOME: Record<string, { label: string; badge: string }> = {
  orders: { label: 'placed orders', badge: 'success' },
  no_change: { label: 'nothing to change', badge: 'neutral' },
  held_stale: { label: 'held — no decisions arrived', badge: 'pending' },
  held_suspect: { label: 'held — the decisions looked wrong', badge: 'error' },
};

const STATUS_BADGE: Record<string, string> = {
  filled: 'success',
  pending: 'pending',
  cancelled: 'neutral',
};

/** The sentence the desk wrote when it raised a finding, or its title. */
function findingText(f: DeskFinding): string {
  return f.payload?.description || f.title || `${f.klass ?? ''} ${f.subject ?? ''}`.trim();
}

/**
 * The score's headline, in words. `label` is the desk's own verdict on how much
 * weight to give the number ("too early", "no evidence yet", "strong"), so it
 * is printed rather than re-interpreted here.
 */
function ScoreCard({ desk }: { desk: DeskState }) {
  const s = desk.score;
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
  const vsBench = s.benchmark_value ? s.value - s.benchmark_value : null;
  return (
    <>
      <div className="stats-bar">
        <div className="stat-item">
          <span className="stat-value">{fmtAmount(s.value)}</span>
          <span className="stat-label">The desk</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{fmtAmount(s.benchmark_value)}</span>
          <span className="stat-label">{s.benchmark} · same money</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{fmtAmount(s.context_value)}</span>
          <span className="stat-label">{s.context} · same money</span>
        </div>
        <div className="stat-item">
          <span
            className="stat-value"
            style={{ color: (vsBench ?? 0) >= 0 ? 'var(--success-text)' : 'var(--danger-text)' }}
          >
            {vsBench === null ? '—' : fmtAmount(vsBench)}
          </span>
          <span className="stat-label">Ahead of {s.benchmark} by</span>
        </div>
      </div>
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
          <div className="meta-row"><span>Costs paid</span><span>{fmtAmount(s.costs)}</span></div>
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
            <span>{fmtAmount(s.after_tax)}</span>
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
            <table className="data-table">
              <thead>
                <tr><th>Symbol</th><th>Day</th><th style={{ textAlign: 'right' }}>Move</th></tr>
              </thead>
              <tbody>
                {s.moves.map(m => (
                  <tr key={`${m.symbol}-${m.day}`}>
                    <td className="mono">{m.symbol}</td>
                    <td className="mono">{m.day}</td>
                    <td className="mono" style={{ textAlign: 'right' }}>{fmtSignedPct(m.move, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}

export default function TradingDesk() {
  const [desk, setDesk] = useState<DeskState | null>(null);
  const [history, setHistory] = useState<DeskHistory | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    let live = true;
    void Promise.allSettled([moneyApi.desk(), moneyApi.deskHistory()]).then(([d, h]) => {
      if (!live) return;
      if (d.status === 'fulfilled') setDesk(d.value);
      else setError((d.reason as Error)?.message || 'Could not read the desk');
      if (h.status === 'fulfilled') setHistory(h.value);
      setLoading(false);
    });
    return () => { live = false; };
  }, []);

  if (loading) return <div className="loading">Reading the desk…</div>;
  if (!desk) return <div className="error">{error || 'Could not read the desk'}</div>;

  const invested = desk.value !== null && desk.cash !== null ? desk.value - desk.cash : null;
  const gainPct = desk.gain !== null && desk.capital ? desk.gain / desk.capital : null;

  return (
    <div>
      <h1 className="page-title">Trading desk</h1>
      <p className="page-subtitle">
        Maou trades the trading system&rsquo;s picks on paper against real closing prices, out
        of {fmtAmount(desk.capital)}, and scores itself against {desk.benchmark}. Everything
        here is a view: the daily run places the orders, and nothing on this page can trade.
      </p>

      {error && <div className="error">{error}</div>}

      {desk.mode !== 'paper' && (
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
          <strong>Mode is &ldquo;{desk.mode}&rdquo;, not paper.</strong>
          <p style={{ margin: '0.4rem 0 0', fontSize: '0.9rem' }}>
            Only paper mode is built. The desk trades nothing until this is set back in the{' '}
            <code>trading-desk-daily</code> config.
          </p>
        </div>
      )}

      <div className="stats-bar">
        <div className="stat-item">
          <span className="stat-value">{fmtAmount(desk.value)}</span>
          <span className="stat-label">Worth today</span>
        </div>
        <div className="stat-item">
          <span
            className="stat-value"
            style={{
              color: (desk.gain ?? 0) >= 0 ? 'var(--success-text)' : 'var(--danger-text)',
            }}
          >
            {fmtAmount(desk.gain)}
          </span>
          <span className="stat-label">
            Up or down on {fmtAmount(desk.capital)}
            {gainPct !== null ? ` · ${fmtSignedPct(gainPct, 1)}` : ''}
          </span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{fmtAmount(invested)}</span>
          <span className="stat-label">In the market</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{fmtPct(desk.cash_pct)}</span>
          <span className="stat-label">Sitting in cash · {fmtAmount(desk.cash)}</span>
        </div>
        <div className="stat-item">
          <span
            className="stat-value"
            style={desk.problems.length ? { color: 'var(--danger-text)' } : undefined}
          >
            {desk.problems.length}
          </span>
          <span className="stat-label">Open complaints</span>
        </div>
      </div>

      <section className="section">
        <div className="section-header-row">
          <h2 className="section-title" style={{ marginBottom: 0 }}>What it holds</h2>
          <span className="meta">as of {desk.as_of}</span>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th style={{ textAlign: 'right' }}>Shares</th>
                <th style={{ textAlign: 'right' }}>Paid each</th>
                <th style={{ textAlign: 'right' }}>Cost</th>
                <th style={{ textAlign: 'right' }}>Last price</th>
                <th style={{ textAlign: 'right' }}>Worth now</th>
                <th style={{ textAlign: 'right' }}>Up or down</th>
                <th style={{ textAlign: 'right' }}>Share of the pot</th>
              </tr>
            </thead>
            <tbody>
              {!desk.positions.length && (
                <tr>
                  <td colSpan={8} className="empty">
                    Nothing held yet — every rupee is still in cash.
                  </td>
                </tr>
              )}
              {desk.positions.map(p => (
                <tr key={p.symbol}>
                  <td>
                    <strong>{p.symbol}</strong>
                    <span className="meta" style={{ marginLeft: 6 }}>{p.asset_class}</span>
                    {!p.priced && (
                      <span className="badge badge-error" style={{ marginLeft: 6 }}>
                        no price — held at cost
                      </span>
                    )}
                  </td>
                  <td className="mono" style={{ textAlign: 'right' }}>{p.qty}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>{fmtAmount(p.avg_cost)}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>{fmtAmount(p.cost)}</td>
                  <td className="mono" style={{ textAlign: 'right' }}>
                    {fmtAmount(p.last_close)}
                    {p.priced_on && p.priced_on !== desk.as_of && (
                      <div className="meta" style={{ fontSize: 11 }}>on {p.priced_on}</div>
                    )}
                  </td>
                  <td className="mono" style={{ textAlign: 'right' }}>{fmtAmount(p.value)}</td>
                  <td
                    className="mono"
                    style={{
                      textAlign: 'right',
                      color: (p.gain ?? 0) >= 0 ? 'var(--success-text)' : 'var(--danger-text)',
                    }}
                  >
                    {fmtAmount(p.gain)}
                  </td>
                  <td className="mono" style={{ textAlign: 'right' }}>{fmtPct(p.weight)}</td>
                </tr>
              ))}
              <tr>
                <td><strong>Cash</strong></td>
                <td colSpan={4} />
                <td className="mono" style={{ textAlign: 'right' }}>{fmtAmount(desk.cash)}</td>
                <td />
                <td className="mono" style={{ textAlign: 'right' }}>{fmtPct(desk.cash_pct)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      {!!desk.pending.length && (
        <section className="section">
          <h2 className="section-title">Waiting to fill</h2>
          <p className="meta" style={{ marginBottom: 10 }}>
            Orders the desk has placed and not yet filled. Each one fills at the next close it
            can price; the money for a buy is already set aside, so today&rsquo;s sizing cannot
            spend it twice.
          </p>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Placed</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th style={{ textAlign: 'right' }}>Shares</th>
                  <th style={{ textAlign: 'right' }}>Priced at</th>
                  <th style={{ textAlign: 'right' }}>About</th>
                </tr>
              </thead>
              <tbody>
                {desk.pending.map(o => (
                  <tr key={o.id}>
                    <td className="mono" style={{ whiteSpace: 'nowrap' }}>{o.created_day}</td>
                    <td><strong>{o.symbol}</strong></td>
                    <td>
                      <span className={`badge badge-${o.side === 'buy' ? 'info' : 'pending'}`}>
                        {o.side}
                      </span>
                    </td>
                    <td className="mono" style={{ textAlign: 'right' }}>{o.qty}</td>
                    <td className="mono" style={{ textAlign: 'right' }}>{fmtAmount(o.ref_price)}</td>
                    <td className="mono" style={{ textAlign: 'right' }}>{fmtAmount(o.est_value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="section">
        <h2 className="section-title">How it is doing</h2>
        <p className="meta" style={{ marginBottom: 12 }}>
          The same figures the monthly close reports: the desk against {desk.benchmark}, the
          halal ETF it is meant to beat, and {desk.context_benchmark} for context. Both
          benchmarks are the same money put in on the desk&rsquo;s first day and held.
        </p>
        <ScoreCard desk={desk} />
      </section>

      <section className="section">
        <h2 className="section-title">What it is complaining about</h2>
        {!desk.problems.length && (
          <div className="empty">Nothing open. The desk has no complaints.</div>
        )}
        <div className="card-vertical-list">
          {desk.problems.map(p => (
            <div className="card" key={p.id}>
              <div className="section-header-row">
                <h3 style={{ marginBottom: 0 }}>{p.title}</h3>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  <span className={`badge badge-${p.severity === 'critical' ? 'error' : 'pending'}`}>
                    {p.severity}
                  </span>
                  <span className="badge badge-neutral">{p.status}</span>
                  {p.muted_until && <span className="badge badge-neutral">muted</span>}
                </div>
              </div>
              {p.description && <p>{p.description}</p>}
              <p className="meta" style={{ marginTop: '0.5rem' }}>
                {p.class} · {p.subject} · seen {p.occurrences}{' '}
                {p.occurrences === 1 ? 'time' : 'times'}, last {p.last_seen_at.slice(0, 16).replace('T', ' ')}
                {p.todoist_task_id && ' · has a Todoist task'}
              </p>
            </div>
          ))}
        </div>
      </section>

      <section className="section">
        <h2 className="section-title">What it did, day by day</h2>
        <p className="meta" style={{ marginBottom: 12 }}>
          One row per decision date the desk reached, whatever it decided — including the days
          it held back, and why. The reasons are the day&rsquo;s own copy: a later rerun cannot
          rewrite what was true that morning.
        </p>
        {!history?.days.length && <div className="empty">The desk has not run yet.</div>}
        <div className="card-vertical-list">
          {(history?.days ?? []).map(day => {
            const o = OUTCOME[day.outcome] ?? { label: day.outcome, badge: 'neutral' };
            return (
              <div className="card" key={day.data_date}>
                <div className="section-header-row">
                  <h3 style={{ marginBottom: 0 }}>{day.data_date}</h3>
                  <span className={`badge badge-${o.badge}`}>{o.label}</span>
                </div>
                {day.findings.map((f, i) => (
                  <p key={i} style={{ color: 'var(--warning-text)' }}>{findingText(f)}</p>
                ))}
                {!!day.skipped.length && (
                  <p className="meta">
                    Left alone because an order was still open: {day.skipped.join(', ')}.
                  </p>
                )}
                {!!day.orders.length && (
                  <div className="table-scroll" style={{ marginTop: '0.6rem' }}>
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
                            <td className="mono" style={{ textAlign: 'right' }}>
                              {fmtAmount(ord.ref_price)}
                            </td>
                            <td>
                              <span className={`badge badge-${STATUS_BADGE[ord.status] ?? 'neutral'}`}>
                                {ord.status}
                              </span>
                              {ord.reason && (
                                <div className="meta" style={{ fontSize: 11 }}>{ord.reason}</div>
                              )}
                            </td>
                            <td className="mono" style={{ textAlign: 'right' }}>
                              {fmtAmount(ord.fill_price)}
                              {ord.fill_date && (
                                <div className="meta" style={{ fontSize: 11 }}>
                                  {ord.fill_date}
                                  {ord.price_source === 'ansaar' ? ' · ansaar price' : ''}
                                </div>
                              )}
                            </td>
                            <td className="mono" style={{ textAlign: 'right' }}>
                              {fmtAmount(ord.costs)}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
