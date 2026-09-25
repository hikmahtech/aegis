import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import { safeHref, timeAgo } from '../lib/feedItems';

// What Raphael's news lane did, so none of it lives only in Slack: the stories
// the morning brief showed (and your 👍/👎), each area's month, and every
// watcher that files items onto a tracked topic. Read-only; the config is on
// the Research page.

const VERDICT: Record<string, string> = { up: '👍', down: '👎' };

function Link({ url, title }: { url: string; title: string }) {
  const href = safeHref(url);
  return href ? <a href={href} target="_blank" rel="noreferrer">{title}</a> : <>{title}</>;
}

export default function News() {
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [stories, setStories] = useState<any[]>([]);
  const [card, setCard] = useState<any[]>([]);
  const [watchers, setWatchers] = useState<any[]>([]);
  const [area, setArea] = useState('');

  async function load() {
    setError(null); setLoading(true);
    try {
      const [s, c, w] = await Promise.all([api.getNewsStories(area), api.getNewsScorecard(), api.getNewsWatchers()]);
      setStories(s.stories || []); setCard(c.areas || []); setWatchers(w.watchers || []);
    } catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, [area]);

  const areas = card.map(c => c.area);

  return (
    <div>
      <h1 className="page-title">News</h1>
      <p className="page-subtitle">
        What Raphael surfaced and how it landed. React 👍 or 👎 on a story in the brief's Slack thread to teach
        the judge. Areas, topics and caps are set on the Research page.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <button className="btn" onClick={() => void load()} disabled={loading}>{loading ? 'Refreshing…' : '↻ Refresh'}</button>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Watchers</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          Scheduled sources that file items onto a tracked topic. Their items reach you only through the area
          that holds the topic. Anything that would stop that is listed in red.
        </p>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr><th>Watcher</th><th>State</th><th>Topic → area</th><th>Last run</th><th>Filed (7d)</th><th>Latest items</th></tr>
            </thead>
            <tbody>
              {watchers.map(w => {
                const run = w.last_run;
                const s = run?.summary || {};
                return (
                  <tr key={w.slug}>
                    <td><div className="mono">{w.slug}</div><div className="meta">{w.schedule}</div></td>
                    <td>
                      <span className={`badge ${w.active ? 'badge-success' : 'badge-pending'}`}>{w.active ? 'on' : 'off'}</span>
                      {(w.problems || []).map((p: string) => <div key={p} className="badge badge-error" style={{ marginTop: 4, whiteSpace: 'normal' }}>{p}</div>)}
                    </td>
                    <td>{w.topic}<div className="meta">{w.area ? `→ ${w.area}` : 'in no area'}</div></td>
                    <td className="meta">
                      {run ? <>{run.status} · {timeAgo(run.started_at)}<br />{s.items !== undefined ? `${s.items} found, ${s.attached ?? 0} new` : ''}</> : 'never ran'}
                    </td>
                    <td>{w.items_7d}</td>
                    <td className="meta">
                      {(w.recent_items || []).map((i: any) => <div key={i.url}><Link url={i.url} title={i.title} /></div>)}
                      {!(w.recent_items || []).length && 'nothing yet'}
                    </td>
                  </tr>
                );
              })}
              {watchers.length === 0 && <tr><td colSpan={6} className="empty">No watchers. A flow whose activities row names a topic appears here.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Areas, last 30 days</h2>
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Area</th><th>Shown</th><th>👍</th><th>👎</th><th>Saved</th><th /></tr></thead>
            <tbody>
              {card.map(c => (
                <tr key={c.area}>
                  <td>{c.area}</td><td>{c.shown}</td><td>{c.up}</td><td>{c.down}</td><td>{c.saved}</td>
                  <td>{c.idle && <span className="badge badge-error">no 👍 or save in 60 days</span>}</td>
                </tr>
              ))}
              {card.length === 0 && <tr><td colSpan={6} className="empty">No stories shown yet.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Stories the brief showed</h2>
        <div style={{ marginBottom: 8 }}>
          <select value={area} onChange={e => setArea(e.target.value)}>
            <option value="">All areas</option>
            {areas.map(a => <option key={a} value={a}>{a}</option>)}
          </select>
        </div>
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th style={{ width: 90 }}>Shown</th><th style={{ width: 160 }}>Area</th><th>Story</th><th style={{ width: 70 }}>You</th></tr></thead>
            <tbody>
              {stories.map((s, i) => (
                <tr key={i}>
                  <td className="meta">{timeAgo(s.shown_at)}</td>
                  <td>{s.area}</td>
                  <td><Link url={s.url} title={s.title} />{s.why && <div className="meta">{s.why}</div>}</td>
                  <td>{VERDICT[s.verdict] || (s.posted ? '—' : <span className="meta">not posted</span>)}</td>
                </tr>
              ))}
              {stories.length === 0 && <tr><td colSpan={4} className="empty">No stories yet. They appear after the next morning brief.</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
