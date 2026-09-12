/**
 * The accounting half of the Money page: where the money stands, the bills,
 * the postings nobody has explained, and the bank statements.
 *
 * Read-only by design. The desk holds real paper positions and the journal is
 * the owner's real accounting, so this panel can show and cannot act.
 *
 * Every figure is reported by whoever already computes it. The balances are
 * hledger's own rendered cells, passed through verbatim; the open bills are the
 * server's `OPEN_DUE_SQL`, the same predicate the month close and the money
 * counter use. Nothing is re-derived here, because a second implementation of a
 * number is how two screens start disagreeing.
 */

import { useEffect, useState } from 'react';
import { fmtMoney } from '../lib/money';
import {
  daysAway,
  moneyApi,
  monthName,
  todoistHref,
  type BalanceReport,
  type Due,
  type MoneyBalances,
  type MoneyDues,
  type MoneyStatements,
  type MoneyUnknowns,
} from '../lib/moneyApi';

type Tab = 'standing' | 'bills' | 'unexplained' | 'statements';

const TABS: Array<[Tab, string]> = [
  ['standing', 'Where it stands'],
  ['bills', 'Bills'],
  ['unexplained', 'Unexplained'],
  ['statements', 'Statements'],
];

/**
 * One hledger report as a table. The balance cell is printed exactly as hledger
 * rendered it — including a mixed-commodity cell like "$-40.00, ₹98,765.50",
 * which means `prices.journal` has no rate for one of those commodities and no
 * single rupee figure is true. Shown as hledger wrote it, the reader can see
 * that; converted to one number, they could not.
 */
function BalanceTable({ report, empty }: { report: BalanceReport; empty: string }) {
  if (!report.rows.length) return <div className="empty">{empty}</div>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>Account</th>
            <th style={{ textAlign: 'right' }}>Balance</th>
          </tr>
        </thead>
        <tbody>
          {report.rows.map(r => (
            <tr key={r.account}>
              <td className="mono">{r.account}</td>
              <td className="mono" style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                {r.balance}
              </td>
            </tr>
          ))}
          {report.total && (
            <tr>
              <td><strong>Total</strong></td>
              <td
                className="mono"
                style={{ textAlign: 'right', whiteSpace: 'nowrap', fontWeight: 650 }}
              >
                {report.total}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function DueTable({ rows, asOf, paid }: { rows: Due[]; asOf: string; paid?: boolean }) {
  if (!rows.length) {
    return <div className="empty">{paid ? 'Nothing settled in this window' : 'Nothing owed'}</div>;
  }
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            <th>{paid ? 'Was due' : 'Due'}</th>
            <th>Who</th>
            <th style={{ textAlign: 'right' }}>Amount</th>
            <th>Books</th>
            <th>{paid ? 'Settled' : 'Task'}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(d => {
            const away = daysAway(d.due_on, asOf);
            const late = !paid && away !== null && away < 0;
            const href = todoistHref(d.todoist_ref);
            return (
              <tr key={d.message_id}>
                <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                  {d.due_on ?? '—'}
                  {!paid && away !== null && (
                    <div className="meta" style={{ fontSize: 11 }}>
                      {late
                        ? `${-away} day${away === -1 ? '' : 's'} late`
                        : away === 0
                          ? 'today'
                          : `in ${away} day${away === 1 ? '' : 's'}`}
                    </div>
                  )}
                </td>
                <td>
                  <strong>{d.payee || '—'}</strong>
                  {late && <span className="badge badge-error" style={{ marginLeft: 6 }}>late</span>}
                  {d.kind === 'failed' && (
                    <span className="badge badge-error" style={{ marginLeft: 6 }}>
                      payment failed
                    </span>
                  )}
                </td>
                <td className="mono" style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {/* An amount the extractor could not read stays blank rather
                      than becoming a confident ₹0. */}
                  {fmtMoney(d.amount, d.currency) || <span className="meta">amount not read</span>}
                </td>
                <td className="meta">{d.entity || '—'}</td>
                <td className="mono" style={{ fontSize: 11 }}>
                  {paid ? (
                    (d.paid_at ?? '').slice(0, 10) || '—'
                  ) : d.todoist_ref ? (
                    href
                      ? <a href={href} target="_blank" rel="noreferrer">task</a>
                      : <span className="meta">task queued</span>
                  ) : (
                    <span className="meta">no task</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function MoneyAccounting() {
  const [tab, setTab] = useState<Tab>('standing');
  const [balances, setBalances] = useState<MoneyBalances | null>(null);
  const [dues, setDues] = useState<MoneyDues | null>(null);
  const [unknowns, setUnknowns] = useState<MoneyUnknowns | null>(null);
  const [statements, setStatements] = useState<MoneyStatements | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    let live = true;
    // allSettled, not all: the balances read a git checkout that can be
    // missing or mid-clone, and that must not take the bills, the unexplained
    // queue and the statements down with it.
    void Promise.allSettled([
      moneyApi.balances(),
      moneyApi.dues(),
      moneyApi.unknowns(),
      moneyApi.statements(),
    ]).then(([bal, due, unk, stmt]) => {
      if (!live) return;
      if (bal.status === 'fulfilled') setBalances(bal.value);
      if (due.status === 'fulfilled') setDues(due.value);
      if (unk.status === 'fulfilled') setUnknowns(unk.value);
      if (stmt.status === 'fulfilled') setStatements(stmt.value);
      const failed = [bal, due, unk, stmt].find(r => r.status === 'rejected');
      if (failed && failed.status === 'rejected') {
        setError((failed.reason as Error)?.message || 'Could not load the books');
      }
      setLoading(false);
    });
    return () => { live = false; };
  }, []);

  if (loading) return <div className="loading">Reading the books…</div>;

  const asOf = dues?.as_of ?? new Date().toISOString().slice(0, 10);
  const unmatched = (statements?.accounts ?? []).reduce((n, a) => n + a.unmatched, 0);

  return (
    <>
      {error && <div className="error">{error}</div>}

      <div className="stats-bar">
        <div className="stat-item">
          <span className="stat-value">{dues?.open.length ?? '—'}</span>
          <span className="stat-label">Bills to pay</span>
        </div>
        <div className="stat-item">
          <span
            className="stat-value"
            style={dues?.overdue_count ? { color: 'var(--danger-text)' } : undefined}
          >
            {dues?.overdue_count ?? '—'}
          </span>
          <span className="stat-label">Overdue</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{unknowns?.rows.length ?? '—'}</span>
          <span className="stat-label">Unexplained · {unknowns?.days ?? 60}d</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{unmatched || '—'}</span>
          <span className="stat-label">Statement lines unmatched</span>
        </div>
      </div>

      <div className="filter-bar" style={{ marginBottom: 14 }}>
        {TABS.map(([key, label]) => (
          <button
            key={key}
            className={`btn ${tab === key ? 'active' : ''}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'standing' && (
        <>
          {balances && !balances.books_ok && (
            <div
              style={{
                background: 'var(--danger-tint)',
                border: '1px solid var(--danger)',
                color: 'var(--danger-text)',
                padding: '10px 14px',
                margin: '0 0 1rem',
                borderRadius: 'var(--radius-sm)',
              }}
            >
              <strong>Could not read the books</strong>
              <p style={{ margin: '0.4rem 0 0', fontSize: '0.9rem' }}>{balances.error}</p>
            </div>
          )}
          <section className="section">
            <h2 className="section-title">What you have and owe</h2>
            <p className="meta" style={{ marginBottom: 10 }}>
              Every asset and liability account, biggest first, converted to{' '}
              {balances?.home_symbol ?? '₹'}. A cell showing two commodities means{' '}
              <code>prices.journal</code> has no rate for one of them, so no single rupee
              figure is true — hledger&rsquo;s own answer is printed rather than a guess.
            </p>
            <BalanceTable
              report={balances?.standing ?? { rows: [], total: null }}
              empty="No balances — the books have nothing in them yet."
            />
          </section>
          <section className="section">
            <h2 className="section-title">
              What changed since {balances ? monthName(balances.month_start) : 'the 1st'} began
            </h2>
            <p className="meta" style={{ marginBottom: 10 }}>
              Income and spending this month, two levels deep. Income shows as a negative
              number: that is the ledger&rsquo;s sign for money coming in, not a loss.
            </p>
            <BalanceTable
              report={balances?.month ?? { rows: [], total: null }}
              empty="Nothing posted this month yet."
            />
          </section>
        </>
      )}

      {tab === 'bills' && (
        <>
          <section className="section">
            <div className="section-header-row">
              <h2 className="section-title" style={{ marginBottom: 0 }}>Still to pay</h2>
              {!!dues?.ticked_off_count && (
                <span className="meta" style={{ maxWidth: 460, textAlign: 'right' }}>
                  {dues.ticked_off_count} more were ticked off in Todoist without a payment ever
                  being matched to them. Those count as paid and are not listed.
                </span>
              )}
            </div>
            <DueTable rows={dues?.open ?? []} asOf={asOf} />
          </section>
          <section className="section">
            <h2 className="section-title">Paid in the last {dues?.paid_days ?? 45} days</h2>
            <p className="meta" style={{ marginBottom: 10 }}>
              A bill lands here when a payment was matched to it in the books — not when its
              task was ticked.
            </p>
            <DueTable rows={dues?.paid_recently ?? []} asOf={asOf} paid />
          </section>
        </>
      )}

      {tab === 'unexplained' && (
        <section className="section">
          <h2 className="section-title">Money the books could not file</h2>
          <p className="meta" style={{ marginBottom: 12 }}>
            Transactions posted to an <code>:unknown</code> account in the last{' '}
            {unknowns?.days ?? 60} days. The money is in the journal; what it was for is not.
            Clearing one means adding a rule or reclassifying it — ask Maou in chat.
          </p>
          <div className="stats-bar">
            {(unknowns?.totals ?? []).map(t => (
              <div className="stat-item" key={`${t.account}/${t.currency}`}>
                <span className="stat-value" style={{ fontSize: '1.15rem' }}>
                  {fmtMoney(t.total, t.currency)}
                </span>
                <span className="stat-label">{t.account} · {t.count}</span>
              </div>
            ))}
            {!unknowns?.totals.length && (
              <div className="stat-item">
                <span className="stat-value">0</span>
                <span className="stat-label">Nothing unexplained</span>
              </div>
            )}
          </div>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Who</th>
                  <th style={{ textAlign: 'right' }}>Amount</th>
                  <th>Filed as</th>
                  <th>Paid by</th>
                  <th>Journal</th>
                </tr>
              </thead>
              <tbody>
                {!unknowns?.rows.length && (
                  <tr><td colSpan={6} className="empty">Nothing unexplained</td></tr>
                )}
                {(unknowns?.rows ?? []).map(r => (
                  <tr key={r.message_id}>
                    <td className="mono" style={{ whiteSpace: 'nowrap' }}>{r.occurred_on ?? '—'}</td>
                    <td><strong>{r.payee || '—'}</strong></td>
                    <td className="mono" style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      {fmtMoney(r.amount, r.currency)}
                    </td>
                    <td className="mono">{r.account}</td>
                    <td className="meta">{r.instrument || r.channel || '—'}</td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      {r.journal_file ?? <span className="meta">not posted</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {unknowns && unknowns.rows.length >= unknowns.limit && (
            <p className="meta" style={{ marginTop: 8 }}>
              Showing the {unknowns.limit} largest. The totals above count every row in the
              window, not only these.
            </p>
          )}
        </section>
      )}

      {tab === 'statements' && (
        <section className="section">
          <h2 className="section-title">Bank statements</h2>
          <p className="meta" style={{ marginBottom: 12 }}>
            A statement is reconciled when its lines have been matched to the books and the
            closing balance agrees. Missing months are checked up to{' '}
            {statements ? monthName(statements.through_month) : 'last month'} — this
            month&rsquo;s statement has not been issued yet.
          </p>
          {!statements?.accounts.length && <div className="empty">No statements imported yet.</div>}
          {(statements?.accounts ?? []).map(acc => (
            <div className="card" key={acc.instrument} style={{ marginBottom: 14 }}>
              <div className="section-header-row">
                <h3 style={{ marginBottom: 0 }}>{acc.instrument}</h3>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <span className={`badge badge-${acc.unmatched ? 'pending' : 'success'}`}>
                    {acc.unmatched
                      ? `${acc.unmatched} of ${acc.rows} lines unmatched`
                      : `all ${acc.rows} lines matched`}
                  </span>
                  <span className="badge badge-neutral">
                    {acc.reconciled_through
                      ? `checked through ${acc.reconciled_through}`
                      : 'never reconciled'}
                  </span>
                </div>
              </div>
              {!!acc.missing_months.length && (
                <p
                  style={{
                    margin: '0 0 0.7rem',
                    fontSize: 'var(--fs-sm)',
                    color: 'var(--warning-text)',
                  }}
                >
                  No statement covering {acc.missing_months.map(monthName).join(', ')}.
                </p>
              )}
              <div className="table-scroll">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Period</th>
                      <th style={{ textAlign: 'right' }}>Opening</th>
                      <th style={{ textAlign: 'right' }}>Closing</th>
                      <th style={{ textAlign: 'right' }}>Lines</th>
                      <th style={{ textAlign: 'right' }}>Unmatched</th>
                      <th>Reconciled</th>
                    </tr>
                  </thead>
                  <tbody>
                    {acc.statements.map(s => (
                      <tr key={s.statement_id}>
                        <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                          {s.period_start} → {s.period_end}
                        </td>
                        <td className="mono" style={{ textAlign: 'right' }}>
                          {fmtMoney(s.opening_balance, 'INR') || '—'}
                        </td>
                        <td className="mono" style={{ textAlign: 'right' }}>
                          {fmtMoney(s.closing_balance, 'INR') || '—'}
                        </td>
                        <td className="mono" style={{ textAlign: 'right' }}>{s.rows}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>{s.unmatched || '—'}</td>
                        <td>
                          {s.reconciled_at ? (
                            <span className="badge badge-success">
                              {s.reconciled_at.slice(0, 10)}
                            </span>
                          ) : (
                            <span className="badge badge-pending">open</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </section>
      )}
    </>
  );
}
