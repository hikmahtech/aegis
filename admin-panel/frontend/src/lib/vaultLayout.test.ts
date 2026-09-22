import { describe, expect, it } from 'vitest';
import {
  changedKeys, formatByTag, joinList, parseByTag, splitList, toSaveBody, type VaultLayout,
} from './vaultLayout';

const kind = {
  enabled: true, folder: '[journal/]YYYY/MM[. ]MMM', format: 'DD MMM YY', live_folder: 'journal',
  template: '_templates/{{tp_title_today}}.md', sections: ['Journal'], label: 'day log',
};
const defaults: VaultLayout = {
  agent_dir: 'raphael',
  questions_dir: 'raphael/questions',
  locale: 'en',
  week_start: 'monday',
  week_numbering: 'iso',
  date_heading_format: 'YYYY-MM-DD',
  index_skip_prefixes: ['.obsidian/', '_templates/'],
  entry: { tag: '#raphael', indent: 'tab', max_outline_depth: 4 },
  new_note: { drop_open_tasks: true, drop_empty_bullets_in_section: true },
  section_ends_at_rule_or_fence: true,
  language: { name: 'English' },
  daily: kind,
  weekly: { ...kind, format: '[W]ww MMM YY', sections: ['Review'], label: 'week in review' },
  monthly: { ...kind, format: 'MM[. ]MMM', live_folder: '', sections: ['Review', 'Month Review'], label: 'month in review' },
  record: { enabled: false, dir: 'me', shared: ['about'], by_tag: {}, max_chars: 6000 },
};

describe('list fields', () => {
  it('split trims, drops empties and joins back', () => {
    expect(splitList(' a, b ,,c ')).toEqual(['a', 'b', 'c']);
    expect(splitList('')).toEqual([]);
    expect(joinList(['Review', 'Month Review'])).toBe('Review, Month Review');
    expect(joinList(undefined)).toBe('');
  });
});

describe('toSaveBody', () => {
  it('drops previous and trims every list', () => {
    const edited: VaultLayout = {
      ...defaults,
      index_skip_prefixes: [' drafts/ ', ''],
      daily: { ...kind, sections: [' Log ', ' '] },
      previous: { ...defaults },
    };
    const body = toSaveBody(edited);
    expect('previous' in body).toBe(false);
    expect(body.index_skip_prefixes).toEqual(['drafts/']);
    expect(body.daily.sections).toEqual(['Log']);
    expect(body.weekly.sections).toEqual(['Review']);
  });
});

describe('changedKeys', () => {
  it('names the top-level keys that differ from the defaults, never previous', () => {
    expect(changedKeys(defaults, defaults)).toEqual([]);
    const edited: VaultLayout = {
      ...defaults,
      agent_dir: 'assistant',
      entry: { ...defaults.entry, indent: 'four_spaces' },
      previous: { ...defaults },
    };
    expect(changedKeys(edited, defaults)).toEqual(['agent_dir', 'entry']);
  });
});

describe('the record map', () => {
  it('parses one line per capability and formats it back', () => {
    const map = parseByTag('finance: money\n gtd : work, people ,\nno colon here\n: orphan');
    expect(map).toEqual({ finance: ['money'], gtd: ['work', 'people'] });
    expect(formatByTag(map)).toBe('finance: money\ngtd: work, people');
    expect(parseByTag(formatByTag(map))).toEqual(map);
  });
  it('toSaveBody trims the shared notes', () => {
    const body = toSaveBody({ ...defaults, record: { ...defaults.record, shared: [' about ', ''] } });
    expect(body.record.shared).toEqual(['about']);
  });
});
