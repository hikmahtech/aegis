// Pure helpers for the Vault page: the layout row as the API returns it, and
// the small conversions the form needs (comma lists, nested edits). The paths
// themselves are rendered on the server (`/api/admin/notes/layout/preview`) —
// nothing here re-implements a date format.

export type KindLayout = {
  enabled: boolean;
  folder: string;
  format: string;
  live_folder: string;
  template: string;
  sections: string[];
  label: string;
};

/** The owner's record (vault record spec §4): the folder, which notes each
 *  capability reads, and the switch that makes it the source of the `user`
 *  persona documents. */
export type RecordLayout = {
  enabled: boolean;
  dir: string;
  shared: string[];
  by_tag: Record<string, string[]>;
  max_chars: number;
};

export type VaultLayout = {
  agent_dir: string;
  questions_dir: string;
  locale: string;
  week_start: string;
  week_numbering: string;
  date_heading_format: string;
  index_skip_prefixes: string[];
  entry: { tag: string; indent: string; max_outline_depth: number };
  new_note: { drop_open_tasks: boolean; drop_empty_bullets_in_section: boolean };
  section_ends_at_rule_or_fence: boolean;
  language: Record<string, string>;
  daily: KindLayout;
  weekly: KindLayout;
  monthly: KindLayout;
  record: RecordLayout;
  previous?: Omit<VaultLayout, 'previous'>;
};

export const KINDS = ['daily', 'weekly', 'monthly'] as const;
export type Kind = (typeof KINDS)[number];

/** `"a, b ,,c"` → `["a", "b", "c"]`: what a comma-separated field saves as. */
export function splitList(text: string): string[] {
  return text.split(',').map(s => s.trim()).filter(Boolean);
}

/** The inverse, for showing a list in one field. */
export function joinList(items: string[] | undefined): string {
  return (items || []).join(', ');
}

/** The body a PUT sends: the layout without `previous` (the server keeps
 *  that itself) and with every list field trimmed. */
export function toSaveBody(layout: VaultLayout): Omit<VaultLayout, 'previous'> {
  const { previous: _previous, ...rest } = layout;
  void _previous;
  return {
    ...rest,
    index_skip_prefixes: rest.index_skip_prefixes.map(s => s.trim()).filter(Boolean),
    daily: { ...rest.daily, sections: rest.daily.sections.map(s => s.trim()).filter(Boolean) },
    weekly: { ...rest.weekly, sections: rest.weekly.sections.map(s => s.trim()).filter(Boolean) },
    monthly: { ...rest.monthly, sections: rest.monthly.sections.map(s => s.trim()).filter(Boolean) },
    record: { ...rest.record, shared: rest.record.shared.map(s => s.trim()).filter(Boolean) },
  };
}

/** The record's capability map as the page edits it: one `tag: a, b` line each. */
export function formatByTag(map: Record<string, string[]>): string {
  return Object.entries(map).map(([tag, names]) => `${tag}: ${names.join(', ')}`).join('\n');
}

/** The inverse. A line with no colon, or no tag before it, is ignored. */
export function parseByTag(text: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const line of text.split('\n')) {
    const i = line.indexOf(':');
    const tag = i < 0 ? '' : line.slice(0, i).trim();
    if (tag) out[tag] = splitList(line.slice(i + 1));
  }
  return out;
}

/** The keys whose values differ from the defaults, top level only — what the
 *  page lists as "changed from the shipped layout". */
export function changedKeys(layout: VaultLayout, defaults: VaultLayout): string[] {
  const out: string[] = [];
  for (const key of Object.keys(defaults) as (keyof VaultLayout)[]) {
    if (key === 'previous') continue;
    if (JSON.stringify(layout[key]) !== JSON.stringify(defaults[key])) out.push(key);
  }
  return out;
}
