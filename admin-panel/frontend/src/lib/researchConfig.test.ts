import { describe, expect, it } from 'vitest';
import {
  numberOrUndefined,
  numbersPayload,
  splitList,
  toAreaRow,
  toAreasPayload,
  toTopicRow,
  toTopicsPayload,
} from './researchConfig';

describe('areas', () => {
  it('round-trips an area through the form', () => {
    const row = toAreaRow({ name: 'India', why: 'I live here', cadence: 'weekly', cap: 2, topics: ['A', 'B'] });
    expect(row).toEqual({ name: 'India', why: 'I live here', cadence: 'weekly', cap: '2', topics: 'A, B' });
    expect(toAreasPayload([row])).toEqual([
      { name: 'India', why: 'I live here', cadence: 'weekly', cap: 2, topics: ['A', 'B'] },
    ]);
  });
  it('omits a blank cap and skips a nameless row', () => {
    const rows = [toAreaRow({ name: 'World' }), { name: ' ', why: '', cadence: 'daily', cap: '', topics: '' }];
    expect(toAreasPayload(rows)).toEqual([{ name: 'World', why: '', cadence: 'daily', topics: [] }]);
  });
});

describe('splitList', () => {
  it('splits on commas and drops blanks', () => {
    expect(splitList(' a, b,, c ')).toEqual(['a', 'b', 'c']);
    expect(splitList('')).toEqual([]);
  });
});

describe('numberOrUndefined', () => {
  it('leaves a blank out so the server default applies', () => {
    expect(numberOrUndefined('')).toBeUndefined();
    expect(numberOrUndefined(null)).toBeUndefined();
    expect(numberOrUndefined(' 7 ')).toBe(7);
    expect(numberOrUndefined('0.6')).toBe(0.6);
  });
  it('passes a non-number through for the server to refuse', () => {
    expect(numberOrUndefined('lots')).toBe('lots');
  });
});

describe('topics', () => {
  it('round-trips a registry entry through the form', () => {
    const row = toTopicRow({ name: 'Rust', queries: ['rust', 'cargo'], priority: 'high', threshold: 4 });
    expect(row).toEqual({ name: 'Rust', queries: 'rust, cargo', priority: 'high', threshold: '4' });
    expect(toTopicsPayload([row])).toEqual({
      topics: [{ name: 'Rust', queries: ['rust', 'cargo'], priority: 'high', threshold: 4 }],
    });
  });
  it('omits a blank threshold and skips a nameless row', () => {
    const rows = [toTopicRow({ name: 'AI' }), { name: '  ', queries: 'x', priority: 'low', threshold: '' }];
    expect(toTopicsPayload(rows)).toEqual({ topics: [{ name: 'AI', queries: [], priority: 'medium' }] });
  });
});

describe('numbersPayload', () => {
  it('keeps only the named keys that hold a value', () => {
    expect(numbersPayload({ a: '3', b: '', c: 'x', d: '1' }, ['a', 'b', 'c'])).toEqual({ a: 3, c: 'x' });
  });
});
