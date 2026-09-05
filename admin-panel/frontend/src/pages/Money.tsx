import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import { fmtMoney } from '../lib/money';

type MoneyEvent = {
  message_id: string;
  mailbox: string;
  entity: string;
  kind: string;
  direction: string | null;
  amount: string | null;
  currency: string | null;
  payee: string | null;
  account: string | null;
  channel: string | null;
  instrument: string | null;
  occurred_on: string | null;
  due_on: string | null;
  parser: string | null;
  confidence: number | null;
  source_class: string | null;
  journal_file: string | null;
  linked_message_id: string | null;
  todoist_ref: string | null;
};

type MoneyState = {
  events: MoneyEvent[];
  unknown_count: number;
  dues_open: number;
  unpushed_commits: number;
  books_configured: boolean;
  home_currency?: string;
};

type Digest = { path: string; markdown: string };

const KIND_BADGE: Record<string, string> = {
  transaction: 'success',
  due: 'pending',
  failed: 'error',
};

/** A Todoist task link, but only for a real task id — an `item-…` ref is a
 *  temp id still sitting in the outbox and Todoist has never seen it. */
function todoistHref(ref: string): string | null {
  return /^\d+$/.test(ref) ? `https://app.todoist.com/app/task/${ref}` : null;
}

export default function Money() {
  const [data, setData] = useState<MoneyState | null>(null);
  const [digest, setDigest] = useState<Digest | null>(null);
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
        The most recent events from the books index. The hledger journal is the record and
        this table is only its index — where the two disagree, the journal file named on the
        row wins.
      </p>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="stats-bar">
        <div className="stat-item">
          <span className="stat-value">{data?.unknown_count ?? '—'}</span>
          <span className="stat-label">Unexplained · 60d</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{data?.dues_open ?? '—'}</span>
          {/* The window is in the label because the month close rendered
              lower down this same page reports its own, month-scoped
              "still open" — two different right answers, three hundred
              pixels apart, on a page whose job is to be trustworthy. */}
          <span className="stat-label">Dues open · all time</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{data?.unpushed_commits ?? '—'}</span>
          <span className="stat-label">Unpushed commits</span>
        </div>
        <div className="stat-item">
          <span className="stat-value">{data?.books_configured ? 'Yes' : 'No'}</span>
          <span className="stat-label">Books configured</span>
        </div>
      </div>

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
            Events are indexed but never posted to a journal, so every amount below is the
            index&rsquo;s own copy with nothing to check it against. Set{' '}
            <code>books_repo_url</code> and the weekly sweep replays the backlog.
          </p>
        </div>
      )}

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

      <section className="section">
        <h2 className="section-title">Latest month close</h2>
        {!digest && (
          <div className="empty" style={{ padding: 16 }}>
            No month close filed yet — run the month close, or wait for the 1st.
          </div>
        )}
        {digest && (
          <div>
            <p className="meta"><span className="mono">{digest.path}</span></p>
            <pre style={{
              whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12,
              maxHeight: 520, overflow: 'auto',
            }}>{digest.markdown}</pre>
          </div>
        )}
      </section>
    </div>
  );
}
