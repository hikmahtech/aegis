import { describe, expect, it } from 'vitest';
import { feedItemsQuery, safeHref, timeAgo } from './feedItems';

describe('feedItemsQuery', () => {
  it('is empty when there is nothing to filter on', () => {
    expect(feedItemsQuery()).toBe('');
    expect(feedItemsQuery({ channelId: '', mode: '', cursor: null })).toBe('');
  });

  it('carries the filters, the page size and the cursor, encoded', () => {
    const q = feedItemsQuery({
      channelId: 'a1b2',
      mode: 'abstract',
      limit: 50,
      cursor: '2026-09-12T13:30:12+00:00|https://x.test/a?b=1',
    });
    const p = new URLSearchParams(q.slice(1));
    expect(q.startsWith('?')).toBe(true);
    expect(p.get('channel_id')).toBe('a1b2');
    expect(p.get('mode')).toBe('abstract');
    expect(p.get('limit')).toBe('50');
    expect(p.get('cursor')).toBe('2026-09-12T13:30:12+00:00|https://x.test/a?b=1');
  });
});

describe('safeHref', () => {
  it('keeps web addresses', () => {
    expect(safeHref('https://example.com/a')).toBe('https://example.com/a');
    expect(safeHref(' HTTP://example.com ')).toBe('HTTP://example.com');
  });

  it('refuses anything a feed could use to run script', () => {
    expect(safeHref('javascript:alert(1)')).toBe('');
    expect(safeHref('data:text/html,hi')).toBe('');
    expect(safeHref('//example.com')).toBe('');
    expect(safeHref(null)).toBe('');
  });
});

describe('timeAgo', () => {
  const now = new Date('2026-09-12T14:00:00Z');

  it('reads like a person would say it', () => {
    expect(timeAgo('2026-09-12T13:59:30Z', now)).toBe('just now');
    expect(timeAgo('2026-09-12T13:55:00Z', now)).toBe('5m ago');
    expect(timeAgo('2026-09-12T11:00:00Z', now)).toBe('3h ago');
    expect(timeAgo('2026-09-10T14:00:00Z', now)).toBe('2d ago');
  });

  it('shows the date once it is a month old, and a dash for nothing', () => {
    expect(timeAgo('2026-07-01T00:00:00Z', now)).toBe('2026-07-01');
    expect(timeAgo(null, now)).toBe('—');
    expect(timeAgo('not a date', now)).toBe('—');
  });
});
