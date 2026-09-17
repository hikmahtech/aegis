import { useState } from 'react';
import { api } from '../api/client';
import { DEFAULT, NONE, describe, toOverrides, toRows, type VerbRow } from '../lib/agentTaskVerbs';
import { useConfigRow } from '../lib/useConfigRow';
import ErrorBanner from './ErrorBanner';

// Source tag → the agent-task lane's verb (`agent_task_verbs`, #344/#558).
// When a task is assigned to an agent, the lane works it by the verb its
// source tag maps to. Every tag has a code default; this panel stores only
// the tags you change.

export default function AgentTaskVerbsPanel() {
  const [cfg, setCfg] = useState<Awaited<ReturnType<typeof api.getAgentTaskVerbs>> | null>(null);
  const [rows, setRows] = useState<VerbRow[]>([]);

  function apply(r: NonNullable<typeof cfg>) {
    setCfg(r);
    setRows(toRows(r.defaults || {}, r.overrides || {}));
  }

  const { error, setError, saving, save } = useConfigRow(async () => {
    apply(await api.getAgentTaskVerbs());
  });

  const onSave = () => save(async () => {
    apply(await api.saveAgentTaskVerbs(toOverrides(rows)));
  }, 'Agent task verbs saved. The next sweep uses them.');

  const defaults = cfg?.defaults || {};
  const label = (v: string | null | undefined) => (v === null ? 'left to you' : v ?? 'none');

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3>Agent task verbs</h3>
      <p className="page-subtitle">
        What an agent does with a task assigned to it, by the task's source tag.{' '}
        <strong>ask</strong> hands it to the agent's own chat; <strong>research</strong> runs a
        cited research pass; <strong>infra</strong>, <strong>email</strong> and{' '}
        <strong>finance</strong> run those lanes. <strong>Left to you</strong> parks the task with
        a note. <code>{cfg?.untagged ?? 'untagged'}</code> is a hand-written task with no tag and
        no <code>@code</code>. Only the tags you change are stored.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <div className="table-scroll">
        <table style={{ width: '100%', fontSize: 13 }}>
          <thead><tr>
            <th style={{ textAlign: 'left' }}>Source tag</th><th>Verb</th>
            <th style={{ textAlign: 'left' }}>What that does</th><th />
          </tr></thead>
          <tbody>
            {rows.map((r, i) => {
              const known = r.tag in defaults;
              return (
                <tr key={i}>
                  <td>
                    {known ? <code>{r.tag}</code> : (
                      <input style={{ width: 120 }} value={r.tag} placeholder="#newtag"
                        onChange={e => setRows(rs => rs.map((x, j) => (j === i ? { ...x, tag: e.target.value } : x)))} />
                    )}
                  </td>
                  <td>
                    <select value={r.choice}
                      onChange={e => setRows(rs => rs.map((x, j) => (j === i ? { ...x, choice: e.target.value } : x)))}>
                      {known && <option value={DEFAULT}>(default: {label(defaults[r.tag])})</option>}
                      {(cfg?.verbs || []).map(v => <option key={v} value={v}>{v}</option>)}
                      <option value={NONE}>left to you</option>
                    </select>
                  </td>
                  <td className="meta">{describe(r.choice, defaults[r.tag])}</td>
                  <td>
                    {!known && (
                      <button className="btn btn-sm" onClick={() => setRows(rs => rs.filter((_, j) => j !== i))}>✕</button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <button className="btn" style={{ marginTop: 8 }} onClick={() => setRows(rs => [...rs, { tag: '', choice: 'ask' }])}>
        + Add tag
      </button>
      <button className="btn btn-primary" style={{ marginTop: 8, marginLeft: 8 }} disabled={saving || !cfg} onClick={onSave}>
        {saving ? 'Saving…' : 'Save verbs'}
      </button>
    </div>
  );
}
