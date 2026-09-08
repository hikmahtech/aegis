import { useEffect, useState } from 'react';
import { api } from '../api/client';

// What the problem hub currently thinks is wrong, and the deploy or
// maintenance windows that are keeping it quiet.
//
// Every mutation here calls the same hub function the chat tools and the
// worker call (`/api/admin/problems/{id}/...`), so the page cannot drift into
// a second idea of what "resolved" means. Nothing here writes Todoist: the
// task is a projection, and the five-minute sweep re-derives it from whatever
// this page changes.

const STATUSES = [
  'open', 'investigating', 'waiting_human', 'fixing', 'verifying', 'suppressed', 'resolved',
];

const SEVERITY_ORDER: Record<string, number> = {
  critical: 0, error: 1, warning: 2, info: 3,
};

const ts = (v: string | null | undefined) =>
  v ? new Date(v).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—';

const ago = (v: string | null | undefined) => {
  if (!v) return '';
  const mins = Math.round((Date.now() - new Date(v).getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
};

// The event kinds, in the words the timeline uses. `state_change` reads as the
// transition it carries rather than as its own kind.
const eventLabel = (e: any): string => {
  const action = (e.payload || {}).action;
  if (e.kind === 'state_change' && action) return String(action);
  return String(e.kind || '');
};

const eventText = (e: any): string => {
  const p = e.payload || {};
  if (p.action === 'grouped') {
    const members = (p.members || []).join(', ');
    return `${p.member_count || 0} folded into one problem${members ? `: ${members}` : ''}`;
  }
  return String(p.text || p.summary || p.reason || p.title || '').slice(0, 400);
};

export default function Problems() {
  const [problems, setProblems] = useState<any[]>([]);
  const [windows, setWindows] = useState<any[]>([]);
  const [counts, setCounts] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState('');
  const [includeClosed, setIncludeClosed] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<any>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [mergeInto, setMergeInto] = useState('');
  const [windowForm, setWindowForm] = useState({ subject: '', state: 'maintenance', minutes: 30, note: '' });

  const load = () => {
    setLoading(true);
    Promise.all([
      api.listProblems({ status: status || undefined, includeClosed }),
      api.listServiceState(),
      api.problemDigest(24),
    ])
      .then(([p, w, d]) => {
        setProblems(p.problems || []);
        setWindows(w.windows || []);
        setCounts(d.counts || null);
      })
      .catch(err => setError(err.message || 'Could not load problems'))
      .finally(() => setLoading(false));
  };

  useEffect(load, [status, includeClosed]);

  const openDetail = (id: string) => {
    if (openId === id) { setOpenId(null); setDetail(null); return; }
    setOpenId(id);
    setDetail(null);
    setMergeInto('');
    api.getProblem(id).then(setDetail).catch(err => setError(err.message || 'Could not load the problem'));
  };

  // Every mutation reloads rather than patching local state: the hub may have
  // changed more than the one field (a resolve sets `resolved_at`, a merge
  // moves events), and showing a guess would be showing something the hub
  // does not say.
  const act = async (fn: () => Promise<any>, label: string) => {
    setBusy(label);
    setError('');
    try {
      await fn();
      load();
      if (openId) api.getProblem(openId).then(setDetail).catch(() => undefined);
    } catch (err: any) {
      setError(err.message || `${label} failed`);
    } finally {
      setBusy('');
    }
  };

  const sorted = [...problems].sort((a, b) => {
    const sev = (SEVERITY_ORDER[a.severity] ?? 9) - (SEVERITY_ORDER[b.severity] ?? 9);
    return sev !== 0 ? sev : String(b.last_seen_at).localeCompare(String(a.last_seen_at));
  });

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">Problems</h1>
          <p className="page-subtitle">
            {loading ? 'loading…' : `${problems.length} ${includeClosed ? 'problems' : 'live problems'}`}
            {counts ? ` · last 24h: ${counts.new} new, ${counts.resolved} resolved, ${counts.occurrences} occurrences` : ''}
          </p>
        </div>
      </div>

      {error && <div className="form-error">{error}</div>}

      {/* Deploy and maintenance windows. While one is in force the hub records
          what it sees for that subject and raises nothing. */}
      <div className="card" style={{ marginBottom: '1rem' }}>
        <div className="section-header-row">
          <h3 style={{ marginBottom: 0 }}>Service state</h3>
          <span className="meta">
            {windows.length ? `${windows.length} window${windows.length === 1 ? '' : 's'} in force` : 'nothing suppressed'}
          </span>
        </div>
        <div>
          {windows.map(w => (
            <div
              key={`${w.subject_kind}:${w.subject}`}
              style={{ display: 'flex', gap: 12, alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}
            >
              <div>
                <code>{w.subject}</code> <span className="badge badge-type">{w.state}</span>{' '}
                <span className="meta">
                  {w.until_at ? `until ${ts(w.until_at)}` : 'until cleared'} · set by {w.set_by}
                  {w.note ? ` · ${w.note}` : ''}
                </span>
              </div>
              <button
                className="btn btn-sm"
                disabled={busy !== ''}
                onClick={() => act(() => api.setServiceState({ subject: w.subject, subject_kind: w.subject_kind, state: 'ok' }), 'clear')}
              >
                Clear
              </button>
            </div>
          ))}
          <div className="form-row" style={{ marginTop: windows.length ? '0.75rem' : 0 }}>
            <div className="form-group" style={{ flex: 2 }}>
              <label>Subject</label>
              <input
                className="mono"
                value={windowForm.subject}
                onChange={e => setWindowForm({ ...windowForm, subject: e.target.value })}
                placeholder="swarm service or node, or * for everything"
              />
            </div>
            <div className="form-group">
              <label>State</label>
              <select value={windowForm.state} onChange={e => setWindowForm({ ...windowForm, state: e.target.value })}>
                <option value="maintenance">maintenance</option>
                <option value="deploying">deploying</option>
                <option value="degraded">degraded</option>
              </select>
            </div>
            <div className="form-group">
              <label>Minutes</label>
              <input
                type="number"
                value={windowForm.minutes}
                onChange={e => setWindowForm({ ...windowForm, minutes: parseInt(e.target.value, 10) || 0 })}
              />
            </div>
            <div className="form-group" style={{ flex: 2 }}>
              <label>Note</label>
              <input
                value={windowForm.note}
                onChange={e => setWindowForm({ ...windowForm, note: e.target.value })}
                placeholder="why — shown on every problem it suppresses"
              />
            </div>
            <div className="form-group" style={{ alignSelf: 'flex-end' }}>
              <button
                className="btn btn-primary"
                disabled={busy !== '' || !windowForm.subject.trim()}
                onClick={() => act(async () => {
                  await api.setServiceState({
                    subject: windowForm.subject.trim(),
                    subject_kind: windowForm.subject.trim() === '*' ? '*' : 'service',
                    state: windowForm.state,
                    minutes: windowForm.minutes,
                    note: windowForm.note,
                  });
                  setWindowForm({ ...windowForm, subject: '', note: '' });
                }, 'window')}
              >
                Open window
              </button>
            </div>
          </div>
        </div>
      </div>

      <div className="filter-bar">
        <select value={status} onChange={e => setStatus(e.target.value)}>
          <option value="">Every status</option>
          {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
          <input type="checkbox" checked={includeClosed} onChange={e => setIncludeClosed(e.target.checked)} />
          Include closed
        </label>
        <span className="meta">Most severe first, then most recently seen.</span>
      </div>

      {!loading && sorted.length === 0 && (
        <p className="meta">Nothing is wrong. {includeClosed ? '' : 'Tick "include closed" for history.'}</p>
      )}

      {sorted.map(p => (
        <div key={p.id} className="card" style={{ marginBottom: '0.5rem' }}>
          <div
            style={{ display: 'flex', gap: 12, alignItems: 'flex-start', justifyContent: 'space-between', cursor: 'pointer' }}
            onClick={() => openDetail(p.id)}
          >
            <div>
              <span className={`badge badge-${p.severity === 'critical' || p.severity === 'error' ? 'error' : 'neutral'}`}>
                {p.severity}
              </span>{' '}
              {p.group_key && (
                <>
                  <span className="badge badge-type" title={
                    `One problem for every ${p.class} on a ${p.subject_kind || 'subject'}. ` +
                    'The next one joins it instead of opening another task.'
                  }>
                    group
                  </span>{' '}
                </>
              )}
              <strong>{p.title}</strong>
              <div className="meta">
                {p.status} ·{' '}
                {p.group_key
                  ? `every ${p.class} on a ${p.subject_kind || 'subject'}`
                  : `${p.subject || '—'} (${p.subject_kind || '—'}) · class ${p.class}`} ·{' '}
                seen {p.occurrences}× · last {ago(p.last_seen_at)}
                {p.muted_until && new Date(p.muted_until) > new Date() ? ` · muted until ${ts(p.muted_until)}` : ''}
              </div>
            </div>
            <span className="meta">{openId === p.id ? '▾' : '▸'}</span>
          </div>

          {openId === p.id && (
            <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
              {!detail && <p className="meta">loading…</p>}
              {detail && detail.problem && detail.problem.id === p.id && (
                <>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 6 }}>
                    <button
                      className="btn btn-sm"
                      disabled={busy !== ''}
                      title="Stop raising this for 24 hours. Occurrences are still recorded and still counted; nothing is investigated and nothing is commented until it lapses."
                      onClick={() => act(() => api.muteProblem(p.id, 24), 'mute')}
                    >
                      Mute 24h
                    </button>
                    <button
                      className="btn btn-sm"
                      disabled={busy !== '' || p.status === 'resolved'}
                      title="Say this is fixed. The task gets a closing comment and is completed; a recurrence within 24 hours reopens this same problem."
                      onClick={() => act(() => api.resolveProblem(p.id, 'resolved from the admin panel'), 'resolve')}
                    >
                      Resolve
                    </button>
                    <button
                      className="btn btn-sm"
                      disabled={busy !== '' || p.status !== 'resolved'}
                      title="Retire a resolved problem now instead of waiting for the nightly sweep. This frees its key, so the same thing breaking again starts a fresh problem rather than reopening this one."
                      onClick={() => act(() => api.closeProblem(p.id), 'close')}
                    >
                      Close
                    </button>
                  </div>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', marginBottom: 12 }}>
                    <span className="meta">Same thing under another name? Paste the duplicate&apos;s id:</span>
                    <input
                      className="mono"
                      style={{ maxWidth: '22rem' }}
                      value={mergeInto}
                      onChange={e => setMergeInto(e.target.value)}
                      placeholder="problem id of the duplicate"
                      aria-label="Problem id of the duplicate to fold into this one"
                    />
                    <button
                      className="btn btn-sm"
                      disabled={busy !== '' || !mergeInto.trim()}
                      title="Move that problem's events, links and sessions onto this one and close it, with a link back. Its own task is completed with a note pointing here."
                      onClick={() => act(async () => {
                        await api.mergeProblems(p.id, mergeInto.trim());
                        setMergeInto('');
                      }, 'merge')}
                    >
                      Fold into this problem
                    </button>
                  </div>

                  <p className="meta">
                    <code>{p.id}</code> · first seen {ts(p.first_seen_at)} · key <code>{p.correlation_key || '(uncorrelated)'}</code>
                  </p>

                  {detail.window && (
                    <p className="meta">
                      Suppressed by <strong>{detail.window.state}</strong> on <code>{detail.window.subject}</code>,
                      {detail.window.until_at ? ` until ${ts(detail.window.until_at)}` : ' until cleared'} (set by {detail.window.set_by}).
                    </p>
                  )}

                  {detail.links.length > 0 && (
                    <p className="meta">
                      Links:{' '}
                      {detail.links.map((l: any, i: number) => (
                        <span key={`${l.link_kind}:${l.ref}`}>
                          {i > 0 ? ' · ' : ''}{l.link_kind}: <code>{l.ref}</code>
                        </span>
                      ))}
                    </p>
                  )}

                  {detail.sessions.length > 0 && (
                    <>
                      <h4>Sessions</h4>
                      {detail.sessions.map((s: any) => (
                        <div key={s.id} className="meta">
                          <strong>{s.owner}</strong> {s.status}
                          {s.account ? ` (${s.account})` : ''} · seen {ago(s.last_seen_at)}
                          {s.summary ? ` · ${s.summary}` : ''}
                          {s.owner === 'aegis' && s.session_id ? (
                            <div><code>cd {s.worktree_path} &amp;&amp; claude --resume {s.session_id}</code></div>
                          ) : null}
                        </div>
                      ))}
                    </>
                  )}

                  <h4>Timeline</h4>
                  {detail.events.length === 0 && <p className="meta">no events</p>}
                  {detail.events.map((e: any) => (
                    <div key={e.id} className="meta">
                      {ts(e.occurred_at)} · <strong>{eventLabel(e)}</strong>/{e.source}
                      {eventText(e) ? ` — ${eventText(e)}` : ''}
                    </div>
                  ))}
                </>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
