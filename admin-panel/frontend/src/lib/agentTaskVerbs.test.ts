import { describe as group, expect, it } from 'vitest';
import { DEFAULT, NONE, choiceOf, describe, toOverrides, toRows } from './agentTaskVerbs';

const defaults = { '#alert': 'infra', '#chat': 'ask', '#money': null };

group('choiceOf', () => {
  it('tells an absent tag (default) from an explicit null (none)', () => {
    expect(choiceOf('#chat', {})).toBe(DEFAULT);
    expect(choiceOf('#chat', { '#chat': null })).toBe(NONE);
    expect(choiceOf('#chat', { '#chat': 'research' })).toBe('research');
  });
});

group('toRows', () => {
  it('lists every default tag, then tags only the overrides name', () => {
    const rows = toRows(defaults, { '#chat': null, '#ops': 'infra' });
    expect(rows).toEqual([
      { tag: '#alert', choice: DEFAULT },
      { tag: '#chat', choice: NONE },
      { tag: '#money', choice: DEFAULT },
      { tag: '#ops', choice: 'infra' },
    ]);
  });
});

group('toOverrides', () => {
  it('saves only real overrides: default is dropped, none is null', () => {
    const out = toOverrides([
      { tag: '#alert', choice: DEFAULT },
      { tag: '#chat', choice: NONE },
      { tag: ' #ops ', choice: 'infra' },
      { tag: '  ', choice: 'ask' },
    ]);
    expect(out).toEqual({ '#chat': null, '#ops': 'infra' });
  });

  it('round-trips what the server returned', () => {
    const overrides = { '#chat': null, '#calendar': 'research' };
    expect(toOverrides(toRows(defaults, overrides))).toEqual(overrides);
  });
});

group('describe', () => {
  it('says what each state does', () => {
    expect(describe(NONE, 'ask')).toMatch(/Left to you/);
    expect(describe(DEFAULT, 'infra')).toBe('Default: infra.');
    expect(describe(DEFAULT, null)).toBe('Default: left to you.');
    expect(describe(DEFAULT, undefined)).toMatch(/undecided/);
    expect(describe('research', 'ask')).toBe('Runs the research verb.');
  });
});
