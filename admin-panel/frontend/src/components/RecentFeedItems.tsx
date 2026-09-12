import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { type FeedItem, feedItemsQuery, safeHref, timeAgo } from '../lib/feedItems';

// The newest RSS entries across the feeds (or one feed), newest first: the
// reading list Miniflux used to be. Read-only. See services/feeds.py::recent_items.

const MODES = ['full', 'abstract', 'failed'] as const;
const MODE_COLORS: Record<string, string> = {
  full: 'var(--success)',
  abstract: 'var(--info)',
  failed: 'var(--danger)',
};
const PAGE = 50;

interface Feed {
  id: string;
  label: string;
}

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message) || fallback;

export default function RecentFeedItems({ feeds }: { feeds: Feed[] }) {
  const [channelId, setChannelId] = useState('');
  const [mode, setMode] = useState('');
  const [items, setItems] = useState<FeedItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  // Set by whatever starts a fetch (first render, a filter change, "Load
  // more"); the effect only ever sets state from the fetch's own callbacks.
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let live = true;
    api.feedItems(feedItemsQuery({ channelId, mode, limit: PAGE }))
      .then(page => {
        if (!live) return;
        setItems(page.items || []);
        setCursor(page.next_cursor || null);
        setError('');
      })
      .catch(e => {
        if (!live) return;
        setItems([]);
        setCursor(null);
        setError(message(e, 'Could not load feed items'));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [channelId, mode]);

  const pickFeed = (value: string) => {
    setLoading(true);
    setChannelId(value);
  };

  const pickMode = (value: string) => {
    setLoading(true);
    setMode(value);
  };

  const loadMore = () => {
    if (!cursor) return;
    setLoading(true);
    api.feedItems(feedItemsQuery({ channelId, mode, limit: PAGE, cursor }))
      .then(page => {
        setItems(prev => [...prev, ...(page.items || [])]);
        setCursor(page.next_cursor || null);
      })
      .catch(e => setError(message(e, 'Could not load more items')))
      .finally(() => setLoading(false));
  };

  return (
    <div style={{ marginTop: 16 }}>
      <div className="page-header-row" style={{ marginBottom: 8 }}>
        <h3 className="section-title" style={{ marginBottom: 0 }}>Recent items</h3>
        <div style={{ display: 'flex', gap: 8 }}>
          <select value={channelId} onChange={e => pickFeed(e.target.value)} aria-label="Feed">
            <option value="">All feeds</option>
            {feeds.map(f => <option key={f.id} value={f.id}>{f.label}</option>)}
          </select>
          <select value={mode} onChange={e => pickMode(e.target.value)} aria-label="Stored as">
            <option value="">Any storage</option>
            {MODES.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 0 }}>
        What the feeds delivered, newest first. "full" stored the page, "abstract" only the
        feed's title and summary, "failed" could not be read. A tick means a prompt has used it.
      </p>
      {error && <div className="form-error">{error}</div>}
      {loading && items.length === 0 ? (
        <div className="loading">Loading items...</div>
      ) : items.length === 0 ? (
        <div className="empty">No items yet</div>
      ) : (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 90 }}>When</th>
                <th style={{ width: 170 }}>Feed</th>
                <th>Item</th>
                <th style={{ width: 80 }}>Stored</th>
                <th style={{ width: 50 }} title="Put into a chat prompt or a research run">Used</th>
              </tr>
            </thead>
            <tbody>
              {items.map(it => {
                const href = safeHref(it.link);
                return (
                  <tr key={`${it.channel_id}:${it.external_id}`}>
                    <td title={it.published || it.seen_at}>{timeAgo(it.seen_at)}</td>
                    <td>{it.feed}</td>
                    <td>
                      {href ? (
                        <a href={href} target="_blank" rel="noopener noreferrer">{it.title}</a>
                      ) : (
                        it.title
                      )}
                      {it.excerpt && (
                        <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>{it.excerpt}</div>
                      )}
                    </td>
                    <td style={{ color: MODE_COLORS[it.mode] || 'inherit' }}>{it.mode}</td>
                    <td>{it.used ? '✓' : '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {cursor && (
        <button className="btn" onClick={loadMore} disabled={loading} style={{ marginTop: 8 }}>
          {loading ? 'Loading...' : 'Load more'}
        </button>
      )}
    </div>
  );
}
