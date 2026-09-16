import { useState } from 'react';
import { api, type EmailTaskLink } from '../api/client';
import DataTable from './DataTable';
import ErrorBanner from './ErrorBanner';
import { useConfigRow } from '../lib/useConfigRow';

// Mail that changes a task AEGIS already tracks (`email_task_links`, #337).
//
// The read path drops a malformed rule with a log line, so until this panel
// existed the rules were edited through the generic settings editor and a
// typo'd action or an unclosed regex saved with a 200 and then matched nothing
// forever. The PUT compiles both regexes server-side and answers 400 with the
// reason, which lands in the banner above.

const BLANK: EmailTaskLink = { key: '', subject_re: '', body_re: '', action: 'complete' };

export default function EmailTaskLinksPanel() {
  const [links, setLinks] = useState<EmailTaskLink[]>([]);
  const [actions, setActions] = useState<string[]>(['complete', 'unblock', 'comment']);

  const { error, setError, saving, save } = useConfigRow(async () => {
    const r = await api.getEmailTaskLinks();
    setLinks(r.links || []);
    setActions(r.actions || []);
  });

  const setRow = (i: number, patch: Partial<EmailTaskLink>) =>
    setLinks(rs => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const onSave = () => save(async () => {
    const r = await api.saveEmailTaskLinks(links);
    setLinks(r.links || []);
  }, 'Task-link rules saved. The next mail is matched against them.');

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2 className="section-title">Mail that closes a task</h2>
      <p className="meta" style={{ marginBottom: 8 }}>
        First match wins. <strong>Subject</strong> finds the task key — group 1 if the pattern has
        one, else the whole match — and the rule applies to the open task whose title contains it.
        <strong> Body</strong> is optional but you almost always want one: an issue tracker sends
        the same subject for <em>every</em> event on a ticket, so subject-only matching would close
        one because somebody commented. Write the body pattern against a real message:
        machine-generated mail glues fields together (<code>Resolution : DoneStatus : Deployed</code>),
        so a <code>\b</code> after the word never matches. Ships empty.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <div className="table-scroll">
        <DataTable
          rows={links}
          emptyText="No rules. Mail never changes a task that already exists."
          columns={[
            {
              header: 'Key',
              th: { style: { textAlign: 'left', width: 140 } },
              cell: (_r, i) => (
                <input style={{ width: '100%' }} value={links[i].key} placeholder="jira-done"
                  onChange={e => setRow(i, { key: e.target.value })} />
              ),
            },
            {
              header: 'Subject pattern',
              th: { style: { textAlign: 'left' } },
              cell: (_r, i) => (
                <input className="mono" style={{ width: '100%' }} value={links[i].subject_re}
                  placeholder="\\((APP-\\d+)\\)"
                  onChange={e => setRow(i, { subject_re: e.target.value })} />
              ),
            },
            {
              header: 'Body pattern',
              th: { style: { textAlign: 'left' } },
              cell: (_r, i) => (
                <input className="mono" style={{ width: '100%' }} value={links[i].body_re || ''}
                  placeholder="resolution\\s*:\\s*Done"
                  onChange={e => setRow(i, { body_re: e.target.value })} />
              ),
            },
            {
              header: 'Action',
              th: { style: { textAlign: 'left', width: 130 } },
              cell: (_r, i) => (
                <select value={links[i].action} onChange={e => setRow(i, { action: e.target.value })}>
                  {actions.map(a => <option key={a} value={a}>{a}</option>)}
                </select>
              ),
            },
            {
              th: { style: { width: 40 } },
              cell: (_r, i) => (
                <button className="btn btn-sm" onClick={() => setLinks(rs => rs.filter((_, j) => j !== i))}>✕</button>
              ),
            },
          ]}
        />
      </div>
      <button className="btn" style={{ marginTop: 8 }} onClick={() => setLinks(rs => [...rs, { ...BLANK }])}>
        + Add rule
      </button>
      <button className="btn btn-primary" style={{ marginTop: 8, marginLeft: 8 }} disabled={saving} onClick={onSave}>
        {saving ? 'Saving…' : 'Save rules'}
      </button>
    </div>
  );
}
