/**
 * Helpers for the Channels page's "Recent items" list
 * (`GET /api/admin/channels/feed-items`, `services/feeds.py::recent_items`).
 */

/** One entry as `recent_items` returns it. */
export interface FeedItem {
  channel_id: string;
  feed: string;
  feed_url: string;
  external_id: string;
  title: string;
  link: string;
  mode: 'full' | 'abstract' | 'failed';
  published: string | null;
  seen_at: string;
  excerpt: string;
  used: boolean;
}

export interface FeedItemsPage {
  items: FeedItem[];
  next_cursor: string | null;
}

export interface FeedItemsQuery {
  channelId?: string;
  mode?: string;
  limit?: number;
  cursor?: string | null;
}

/** The query string for one page of recent items ("" when there is nothing to send). */
export function feedItemsQuery(q: FeedItemsQuery = {}): string {
  const p = new URLSearchParams();
  if (q.channelId) p.set('channel_id', q.channelId);
  if (q.mode) p.set('mode', q.mode);
  if (q.limit) p.set('limit', String(q.limit));
  if (q.cursor) p.set('cursor', q.cursor);
  const s = p.toString();
  return s ? `?${s}` : '';
}

/**
 * The link to render as an `href`, or "" when it is not a web address. The
 * server already drops anything else; a feed is untrusted input, so the page
 * checks again rather than render a `javascript:` link it was handed.
 */
export function safeHref(link: string | null | undefined): string {
  const s = (link || '').trim();
  return /^https?:\/\//i.test(s) ? s : '';
}

/** "just now", "5m ago", "3h ago", "2d ago", else the date. */
export function timeAgo(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '—';
  const secs = Math.max(0, Math.round((now.getTime() - t) / 1000));
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return new Date(t).toISOString().slice(0, 10);
}
