import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from './ErrorBanner';
import { toast } from './Toast';

// How a chat turn ranks what the knowledge store finds (`knowledge_ranking`,
// #579). A document's score is (similarity + domain boost) × decay × weight,
// and both the threshold and the order read it. Each field overrides the
// shipped registry (services/source_types.py); a blank field keeps it.

type Entry = { rank_boost?: number; decay_days?: number | null };
type Registry = Record<string, { rank_boost: number; decay_days: number | null; description: string }>;
type Row = { type: string; rank: string; decay: string };
type Ranking = Awaited<ReturnType<typeof api.getKnowledgeRanking>>;

const TYPE_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;

function rowsFrom(registry: Registry, overrides: Record<string, Entry>): Row[] {
  const types = Array.from(new Set([...Object.keys(registry), ...Object.keys(overrides)])).sort();
  return types.map(type => {
    const o = overrides[type] || {};
    let decay = '';
    if (o.decay_days === null) decay = 'default';
    else if (o.decay_days !== undefined) decay = String(o.decay_days);
    return { type, rank: o.rank_boost === undefined ? '' : String(o.rank_boost), decay };
  });
}

export default function KnowledgeRankingPanel() {
  const [data, setData] = useState<Ranking | null>(null);
  const [boost, setBoost] = useState('');
  const [rows, setRows] = useState<Row[]>([]);
  const [newType, setNewType] = useState('');
  const [error, setError] = useState<Error | null>(null);
  const [saving, setSaving] = useState(false);

  function load(r: Ranking) {
    setData(r);
    setBoost(r.domain_boost === r.defaults.domain_boost ? '' : String(r.domain_boost));
    setRows(rowsFrom(r.registry, r.source_types));
  }

  useEffect(() => {
    api.getKnowledgeRanking().then(load).catch((e: any) => setError(e));
  }, []);

  function setRow(i: number, patch: Partial<Row>) {
    setRows(x => x.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }

  function addType() {
    const t = newType.trim();
    if (!TYPE_RE.test(t)) {
      setError(new Error(`"${t}" is not a source type: use lowercase letters, digits, _ . or -`));
      return;
    }
    if (!rows.some(r => r.type === t)) {
      setRows(x => [...x, { type: t, rank: '', decay: '' }].sort((a, b) => a.type.localeCompare(b.type)));
    }
    setNewType('');
  }

  async function save() {
    if (!data) return;
    setSaving(true); setError(null);
    try {
      const source_types: Record<string, Entry> = {};
      for (const r of rows) {
        const e: Entry = {};
        const rank = r.rank.trim();
        if (rank !== '') {
          const n = Number(rank);
          if (!Number.isFinite(n)) throw new Error(`${r.type}: the weight must be a number`);
          e.rank_boost = n;
        }
        const decay = r.decay.trim().toLowerCase();
        if (decay === 'default') e.decay_days = null;
        else if (decay !== '') {
          const n = Number(decay);
          if (!Number.isInteger(n)) {
            throw new Error(`${r.type}: decay must be a whole number of days, or "default"`);
          }
          e.decay_days = n;
        }
        if (Object.keys(e).length) source_types[r.type] = e;
      }
      let domain_boost = data.defaults.domain_boost;
      if (boost.trim() !== '') {
        domain_boost = Number(boost);
        if (!Number.isFinite(domain_boost)) throw new Error('The domain boost must be a number');
      }
      load(await api.saveKnowledgeRanking({ domain_boost, source_types }));
      toast.ok('Ranking saved. The next chat turn uses it.');
    } catch (e: any) { setError(e); } finally { setSaving(false); }
  }

  if (!data) {
    return (
      <div className="card" style={{ marginTop: 16 }}>
        <h3>Ranking</h3>
        <ErrorBanner error={error} onDismiss={() => setError(null)} />
      </div>
    );
  }

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h3>Ranking</h3>
      <p className="page-subtitle">
        How a chat turn picks what it is shown. A document scores (similarity + domain boost) ×
        decay × weight, and only scores over the threshold reach the prompt, best first. The
        domain boost goes to an agent's own source types (its knowledge domains on the Agents →
        Behavior tab). Leave a field blank to keep the shipped value shown in grey.
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />
      <div className="cfg-row" style={{ marginBottom: 12 }}>
        <span className="cfg-label">Domain boost</span>
        <input type="number" step="0.05" min={0} max={data.limits.domain_boost} value={boost}
          placeholder={String(data.defaults.domain_boost)} style={{ width: 120 }}
          onChange={e => setBoost(e.target.value)} />
        <span className="meta">added to similarity, 0 to {data.limits.domain_boost}</span>
      </div>
      <div className="table-scroll">
        <table style={{ width: '100%' }}>
          <thead>
            <tr>
              <th style={{ textAlign: 'left' }}>Source type</th>
              <th>Weight (×)</th>
              <th>Decay (days)</th>
              <th style={{ textAlign: 'left' }}>What it is</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const reg = data.registry[r.type];
              const rankPlaceholder = String(reg ? reg.rank_boost : 1);
              const decayPlaceholder = reg && reg.decay_days !== null
                ? String(reg.decay_days)
                : `default (${data.defaults.decay_days})`;
              return (
                <tr key={r.type}>
                  <td><code>{r.type}</code></td>
                  <td style={{ textAlign: 'center' }}>
                    <input type="text" inputMode="decimal" value={r.rank} placeholder={rankPlaceholder}
                      style={{ width: 80 }} onChange={e => setRow(i, { rank: e.target.value })} />
                  </td>
                  <td style={{ textAlign: 'center' }}>
                    <input type="text" value={r.decay} placeholder={decayPlaceholder}
                      style={{ width: 110 }} onChange={e => setRow(i, { decay: e.target.value })} />
                  </td>
                  <td className="meta">{reg ? reg.description : 'Not in the shipped registry.'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="meta" style={{ marginTop: 8 }}>
        Weight: 0 to {data.limits.rank_boost}; 0 keeps a type out of prompts. Decay: 1 to{' '}
        {data.limits.decay_days} days, or <code>default</code> for {data.defaults.decay_days}.
      </p>
      <div className="cfg-row" style={{ marginTop: 8 }}>
        <input value={newType} placeholder="another source type, e.g. sentry"
          onChange={e => setNewType(e.target.value)} onKeyDown={e => e.key === 'Enter' && addType()} />
        <button className="btn" disabled={!newType.trim()} onClick={addType}>Add type</button>
      </div>
      <button className="btn btn-primary" style={{ marginTop: 8 }} disabled={saving} onClick={save}>
        {saving ? 'Saving…' : 'Save ranking'}
      </button>
    </div>
  );
}
