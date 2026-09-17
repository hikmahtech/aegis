import { useState } from 'react';
import { api } from '../api/client';
import { useConfigRow } from '../lib/useConfigRow';
import ErrorBanner from './ErrorBanner';

// Who "you" are in a meeting transcript (`meeting_rules.self_names`, #558).
// Empty means meeting notes are filed but the self-review is skipped, so this
// decides whether a review happens at all. Ships empty: a fork names nobody.

export default function MeetingNamesPanel() {
  const [names, setNames] = useState<string[]>([]);
  const { error, setError, saving, save } = useConfigRow(async () => {
    setNames((await api.getMeetingRules()).self_names || []);
  });

  const onSave = () => save(async () => {
    const r = await api.saveMeetingRules({ self_names: names.map(n => n.trim()).filter(Boolean) });
    setNames(r.self_names || []);
  }, 'Meeting names saved. The next meeting notes use them.');

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2 className="section-title">Your name in meeting transcripts</h2>
      <p className="meta" style={{ marginBottom: 8 }}>
        How you appear as a speaker in meeting notes (a sender rule tagged <code>meeting</code> sends
        them here). Matched case-insensitively, and a part is enough: <code>Sam</code> matches{' '}
        <code>Sam Doe</code>. With no name, notes are still filed but the talk-share numbers and
        the self-review are skipped.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      {names.map((n, i) => (
        <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 6 }}>
          <input type="text" value={n} placeholder="Sam Doe" style={{ flex: 1 }}
            onChange={e => setNames(x => x.map((v, j) => (j === i ? e.target.value : v)))} />
          <button className="btn btn-sm" onClick={() => setNames(x => x.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      {names.length === 0 && <div className="empty">No names. Meeting reviews are skipped.</div>}
      <button className="btn" style={{ marginTop: 8 }} onClick={() => setNames(x => [...x, ''])}>
        + Add name
      </button>
      <button className="btn btn-primary" style={{ marginTop: 8, marginLeft: 8 }} disabled={saving} onClick={onSave}>
        {saving ? 'Saving…' : 'Save names'}
      </button>
    </div>
  );
}
