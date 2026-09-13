// Pure helpers for the Research page: form text <-> the JSON the admin API
// takes. Kept out of the component so they can be unit-tested.

/** "a, b,, c " -> ["a", "b", "c"]. */
export function splitList(text: string): string[] {
  return text
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
}

/** A number typed in a form field, or `undefined` when blank so the server's
 *  default applies. Anything else is passed through as typed (the server
 *  validates loudly and the page shows its 400). */
export function numberOrUndefined(text: string | number | null | undefined): number | string | undefined {
  if (text === null || text === undefined) return undefined;
  const s = String(text).trim();
  if (s === '') return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : s;
}

export type TopicRow = {
  name: string;
  queries: string; // comma-separated in the form
  priority: string;
  threshold: string; // blank = the priority's number
};

/** One registry entry as the form edits it. */
export function toTopicRow(t: any): TopicRow {
  return {
    name: String(t?.name ?? ''),
    queries: Array.isArray(t?.queries) ? t.queries.join(', ') : '',
    priority: String(t?.priority ?? 'medium'),
    threshold: t?.threshold === undefined || t?.threshold === null ? '' : String(t.threshold),
  };
}

/** The PUT body for the topics registry. A blank threshold is left out, so
 *  the priority's number applies again. */
export function toTopicsPayload(rows: TopicRow[]): { topics: any[] } {
  return {
    topics: rows
      .filter(r => r.name.trim())
      .map(r => {
        const entry: any = { name: r.name.trim(), queries: splitList(r.queries), priority: r.priority };
        const threshold = numberOrUndefined(r.threshold);
        if (threshold !== undefined) entry.threshold = threshold;
        return entry;
      }),
  };
}

/** A form of numeric fields -> the config object, blanks dropped. */
export function numbersPayload(form: Record<string, string>, keys: string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of keys) {
    const v = numberOrUndefined(form[k]);
    if (v !== undefined) out[k] = v;
  }
  return out;
}
