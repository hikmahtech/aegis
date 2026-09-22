import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import { useConfigRow } from '../lib/useConfigRow';
import ErrorBanner from '../components/ErrorBanner';
import { toast } from '../components/Toast';
import {
  changedKeys, joinList, KINDS, splitList, toSaveBody,
  type Kind, type VaultLayout,
} from '../lib/vaultLayout';
import DataTable from '../components/DataTable';

// The vault layout — where the journal notes go, what an entry looks like —
// and the user's clock. AEGIS ships one vault's conventions as the defaults;
// yours live in the `vault_layout` settings row. The preview is rendered on
// the server by the code that writes the notes, so what it shows is what the
// nightly run will do.

type Options = Record<string, string[]>;
type Preview = {
  date: string;
  week: { start: string; end: string; label: string };
  heading: string;
  sample_block: string;
  daily: KindPreview; weekly: KindPreview; monthly: KindPreview;
};
type KindPreview = { enabled: boolean; path: string; live_path: string; template: string; sections: string[] };

const KIND_HELP: Record<Kind, string> = {
  daily: 'The nightly day log. Folder and name are rendered for the day.',
  weekly: 'The weekly rollup. Rendered for the first day of the week (the week rule above).',
  monthly: 'The monthly rollup. Rendered for the first of the month.',
};
const LANGUAGE_HELP: Record<string, string> = {
  name: 'The language the day log and rollups are written in. English adds nothing to the prompts.',
  daylog_title: 'First line of a day log written without a model. {date} is the day.',
  quiet_day: 'Said after the title on a day with nothing recorded.',
  rollup_header: 'First line of a rollup written without a model: {period}, {label}, {n}.',
  journal_title: 'How a journal note is titled inside a rollup. {day}.',
  also_in_note: 'Introduces what else the note held, after the agent\'s own block.',
  review_label: 'The label on the weekly review block, filed in the week\'s note.',
  selfreport_label: 'The label on your answer to the journal prompt, filed in the day\'s note.',
};

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

export default function Vault() {
  const [layout, setLayout] = useState<VaultLayout | null>(null);
  const [defaults, setDefaults] = useState<VaultLayout | null>(null);
  const [options, setOptions] = useState<Options>({});
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [date, setDate] = useState(today());
  const [timezone, setTimezone] = useState('');
  const [effectiveTz, setEffectiveTz] = useState('UTC');
  const [showWording, setShowWording] = useState(false);

  const { error, setError, loading, saving, save: saveRow } = useConfigRow(async () => {
    const r = await api.getVaultLayout();
    setLayout(r.layout); setDefaults(r.defaults); setOptions(r.options || {});
    const tz = await api.getTimezone();
    setTimezone(tz.timezone || ''); setEffectiveTz(tz.effective || 'UTC');
  });

  // Live preview of the layout AS EDITED, debounced; a layout that does not
  // validate shows the server's reason instead of a stale preview.
  useEffect(() => {
    if (!layout) return;
    const handle = setTimeout(async () => {
      try {
        setPreview(await api.previewVaultLayout(toSaveBody(layout), date));
        setPreviewError(null);
      } catch (e: any) {
        setPreviewError(e?.message || String(e));
      }
    }, 400);
    return () => clearTimeout(handle);
  }, [layout, date]);

  // The guard stays outside `saveRow`: with no layout loaded there is nothing
  // to write, and nothing to say was saved.
  const save = () => layout && saveRow(async () => {
    setLayout((await api.saveVaultLayout(toSaveBody(layout))).layout);
  }, 'Vault layout saved — applies within ~30s. Existing notes stay where they are.');

  async function saveTz() {
    setError(null);
    try {
      const r = await api.saveTimezone(timezone);
      setTimezone(r.timezone); setEffectiveTz(r.effective);
      toast.ok(`Timezone saved: ${r.effective}.`);
    } catch (e: any) { setError(e); }
  }

  const changed = useMemo(
    () => (layout && defaults ? changedKeys(layout, defaults) : []),
    [layout, defaults],
  );

  function set<K extends keyof VaultLayout>(key: K, value: VaultLayout[K]) {
    setLayout(l => (l ? { ...l, [key]: value } : l));
  }
  function setKind(kind: Kind, patch: Partial<VaultLayout[Kind]>) {
    setLayout(l => (l ? { ...l, [kind]: { ...l[kind], ...patch } } : l));
  }

  const row = (label: string, control: React.ReactNode, help?: string) => (
    <tr>
      <td style={{ width: 220 }}><label>{label}</label></td>
      <td>{control}</td>
      <td className="meta" style={{ width: 360 }}>{help || ''}</td>
    </tr>
  );
  const select = (value: string, opts: string[], onChange: (v: string) => void) => (
    <select value={value} onChange={e => onChange(e.target.value)}>
      {(opts.length ? opts : [value]).map(o => <option key={o} value={o}>{o}</option>)}
    </select>
  );
  const text = (value: string, onChange: (v: string) => void, placeholder = '', mono = true) => (
    <input
      type="text" value={value} placeholder={placeholder}
      onChange={e => onChange(e.target.value)}
      style={{ width: '100%', fontFamily: mono ? 'var(--mono)' : undefined }}
    />
  );

  return (
    <div>
      <h1 className="page-title">Vault</h1>
      <p className="page-subtitle">
        Where your research agent files the journal in your Obsidian vault, and what its entries
        look like. AEGIS ships one vault's conventions as the defaults; yours are saved here.
        The repo and deploy key are on the Integrations page.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {loading && <div className="loading">Loading the layout…</div>}

      <div className="card" style={{ marginTop: 16 }}>
        <h2 className="section-title">Your clock</h2>
        <p className="meta" style={{ marginBottom: 8 }}>
          <strong>Global</strong>, not just the vault: the day log bounds its day on this clock,
          a dated heading and a template's date use it, and so does "today" in chat. Empty means
          UTC. Use a zone name such as <code>Europe/Berlin</code> or <code>Asia/Kolkata</code>.
          Set the nightly day-log cron (Flows page) to just after midnight in this zone.
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input
            type="text" value={timezone} placeholder="Europe/Berlin"
            onChange={e => setTimezone(e.target.value)} style={{ width: 260 }}
          />
          <button className="btn btn-sm btn-primary" onClick={saveTz}>Save timezone</button>
          <span className="meta">In force: <code>{effectiveTz}</code></span>
        </div>
      </div>

      {layout && (
        <>
          <div className="card" style={{ marginTop: 16 }}>
            <h2 className="section-title">Preview</h2>
            <p className="meta" style={{ marginBottom: 8 }}>
              Rendered by the server for the layout as you have edited it (unsaved edits
              included), for the date below. Changing the layout never moves an existing note:
              a day already written under the previous layout is recognised there and is never
              written twice.
            </p>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }}>
              <label>Date</label>
              <input type="date" value={date} onChange={e => setDate(e.target.value)} />
              {preview && <span className="meta">
                week {preview.week.label}: {preview.week.start} to {preview.week.end}; heading <code>{preview.heading}</code>
              </span>}
            </div>
            {previewError && <ErrorBanner error={previewError} />}
            {preview && !previewError && (
              <div className="table-scroll">
                <DataTable
                  rows={[...KINDS]}
                  rowKey={k => k}
                  tr={k => ({ style: { opacity: preview[k].enabled ? 1 : 0.5 } })}
                  columns={[
                    {
                      header: 'Note',
                      th: { style: { width: 90 } },
                      cell: k => <>{k}{!preview[k].enabled && ' (off)'}</>,
                    },
                    { header: 'Filed at', cell: k => <code>{preview[k].path}</code> },
                    {
                      header: 'Live note (appended to when it exists)',
                      cell: k => <code>{preview[k].live_path || '—'}</code>,
                    },
                    { header: 'Section', cell: k => preview[k].sections.join(' / ') },
                  ]}
                />
                <pre style={{ marginTop: 8, fontSize: 12 }}>{preview.sample_block}</pre>
              </div>
            )}
          </div>

          <div className="card" style={{ marginTop: 16 }}>
            <h2 className="section-title">Folders and rules</h2>
            <div className="table-scroll">
              <table className="data-table">
                <tbody>
                  {row('Agent folder', text(layout.agent_dir, v => set('agent_dir', v)),
                    'The only folder the agent may write its own notes in (note_write, note_link). One folder name.')}
                  {row('Questions folder', text(layout.questions_dir, v => set('questions_dir', v)),
                    'Where research answers are filed. Inside the agent folder. Never indexed (the answers are already in the store).')}
                  {row('Locale', select(layout.locale, options.locale || [], v => set('locale', v)),
                    'Month and day names for MMM, MMMM, ddd, dddd.')}
                  {row('Week starts on', select(layout.week_start, options.week_start || [], v => set('week_start', v)),
                    'The weekly rollup covers this week, and the weekly note is named from its first day.')}
                  {row('Week numbering', select(layout.week_numbering, options.week_numbering || [], v => set('week_numbering', v)),
                    'iso: week 1 holds January 4th. locale_us: week 1 holds January 1st. What ww renders.')}
                  {row('Date heading', text(layout.date_heading_format, v => set('date_heading_format', v)),
                    'The heading a dated section gets (note_write with no heading, a research answer). A moment.js format.')}
                  {row('Index skips', text(joinList(layout.index_skip_prefixes), v => set('index_skip_prefixes', splitList(v))),
                    'Path prefixes the index leaves out, comma-separated.')}
                  {row('Entry tag', text(layout.entry.tag, v => set('entry', { ...layout.entry, tag: v })),
                    'The tag on the agent\'s bullet (#tag), or empty for none.')}
                  {row('Entry indent', select(layout.entry.indent, options.indent || [], v => set('entry', { ...layout.entry, indent: v })),
                    'How the outline under the bullet is indented — match your outliner.')}
                  {row('Outline depth', (
                    <input
                      type="number" min={1} max={10} value={layout.entry.max_outline_depth}
                      onChange={e => set('entry', { ...layout.entry, max_outline_depth: Number(e.target.value) || 1 })}
                      style={{ width: 80 }}
                    />
                  ), 'How deep the outline may nest.')}
                  {row('New note', (
                    <span style={{ display: 'flex', gap: 16 }}>
                      <label><input type="checkbox" checked={layout.new_note.drop_open_tasks}
                        onChange={e => set('new_note', { ...layout.new_note, drop_open_tasks: e.target.checked })} /> drop open checkboxes</label>
                      <label><input type="checkbox" checked={layout.new_note.drop_empty_bullets_in_section}
                        onChange={e => set('new_note', { ...layout.new_note, drop_empty_bullets_in_section: e.target.checked })} /> drop the section's empty bullets</label>
                    </span>
                  ), 'What is removed from the template when the agent creates a note. An existing note is never changed.')}
                  {row('Section ends at', (
                    <label><input type="checkbox" checked={layout.section_ends_at_rule_or_fence}
                      onChange={e => set('section_ends_at_rule_or_fence', e.target.checked)} /> a --- rule or a code fence, as well as the next heading</label>
                  ), 'Where the agent\'s block goes: the end of the section, which ends here.')}
                </tbody>
              </table>
            </div>
          </div>

          {KINDS.map(k => (
            <div className="card" style={{ marginTop: 16 }} key={k}>
              <h2 className="section-title">
                {k[0].toUpperCase() + k.slice(1)} note{' '}
                <label className="meta" style={{ fontWeight: 'normal', marginLeft: 12 }}>
                  <input type="checkbox" checked={layout[k].enabled}
                    onChange={e => setKind(k, { enabled: e.target.checked })} /> enabled
                </label>
              </h2>
              <p className="meta" style={{ marginBottom: 8 }}>
                {KIND_HELP[k]} Formats are moment.js: <code>YYYY MM DD</code>, <code>MMM</code>,
                {' '}<code>ww</code>, <code>[literal text]</code>. Off = the entry is filed as a knowledge row instead.
              </p>
              <div className="table-scroll">
                <table className="data-table">
                  <tbody>
                    {row('Folder', text(layout[k].folder, v => setKind(k, { folder: v }), '[journal/]YYYY/MM[. ]MMM'),
                      'Rendered for the date. Bracket every literal: a bare letter is a token. Empty = the vault root.')}
                    {row('File name', text(layout[k].format, v => setKind(k, { format: v }), 'DD MMM YY'),
                      'Without .md. A day needs a day token and a month or year; a week needs a week or day token.')}
                    {row('Live folder', text(layout[k].live_folder, v => setKind(k, { live_folder: v }), 'journal'),
                      'Where your periodic-notes plugin creates the note before you file it. A live note is written into when it exists, never created. Empty = none.')}
                    {row('Template', text(layout[k].template, v => setKind(k, { template: v }), '_templates/day.md'),
                      'Rendered for a note the agent creates ({{date:FMT}}, {{time}}, {{title}}; Templater tags are dropped). Empty = a title line only.')}
                    {row('Sections', text(joinList(layout[k].sections), v => setKind(k, { sections: splitList(v) }), 'Journal', false),
                      'Heading texts, in order of preference; the block goes at the end of the first one the note has, at any level. Added as ## when missing.')}
                    {row('Label', text(layout[k].label, v => setKind(k, { label: v }), 'day log', false),
                      'The words after the tag on the bullet.')}
                  </tbody>
                </table>
              </div>
            </div>
          ))}

          <div className="card" style={{ marginTop: 16 }}>
            <h2 className="section-title">
              Wording{' '}
              <button className="btn btn-sm" style={{ marginLeft: 12 }} onClick={() => setShowWording(s => !s)}>
                {showWording ? 'hide' : 'show'}
              </button>
            </h2>
            <p className="meta" style={{ marginBottom: 8 }}>
              The language the entries are written in, and the fixed words of a day log written
              without a model.
            </p>
            {showWording && (
              <div className="table-scroll">
                <table className="data-table">
                  <tbody>
                    {(options.language_keys || Object.keys(layout.language)).map(key => row(
                      key,
                      text(layout.language[key] ?? '', v => set('language', { ...layout.language, [key]: v }), '', false),
                      LANGUAGE_HELP[key] || 'A label in the day log written without a model.',
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div style={{ marginTop: 16, display: 'flex', gap: 12, alignItems: 'center' }}>
            <button className="btn btn-primary" disabled={saving} onClick={save}>
              {saving ? 'Saving…' : 'Save layout'}
            </button>
            {defaults && (
              <button className="btn" onClick={() => setLayout({ ...defaults, previous: layout.previous })}>
                Reset to the shipped defaults
              </button>
            )}
            <span className="meta">
              {changed.length ? `Changed from the defaults: ${changed.join(', ')}` : 'The shipped layout, unchanged.'}
              {layout.previous && ' A previous layout is kept: its notes still count as written.'}
            </span>
          </div>
        </>
      )}
    </div>
  );
}
