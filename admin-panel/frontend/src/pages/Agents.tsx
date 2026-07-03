import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type Agent = {
  id: string;
  name: string;
  role: string;
  capabilities?: string[];
  model_tier?: string;
  active?: boolean;
};

const slugify = (s: string) =>
  s.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

export default function Agents() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ id: '', name: '', role: '', model_tier: 'balanced' });
  const [idEdited, setIdEdited] = useState(false);
  const [saving, setSaving] = useState(false);

  function load() {
    api
      .listAgents()
      .then(data => { setAgents(data || []); setLoading(false); })
      .catch(err => { setError(err); setLoading(false); });
  }
  useEffect(load, []);

  // Auto-derive id from name until the user edits the id field directly.
  function setName(name: string) {
    setForm(f => ({ ...f, name, id: idEdited ? f.id : slugify(name) }));
  }

  async function create() {
    setSaving(true); setError(null);
    try {
      const a = await api.createAgent({
        id: form.id.trim(),
        name: form.name.trim(),
        role: form.role.trim(),
        model_tier: form.model_tier,
      });
      // Straight to the detail page to fill in persona (draft-with-AI / edit).
      navigate(`/agents/${a.id}`);
    } catch (err: any) {
      setError(err);
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="loading">Loading agents…</div>;

  const canCreate = form.id.trim() && form.name.trim() && form.role.trim();

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
        <div>
          <h1 className="page-title">Agents</h1>
          <p className="page-subtitle">Who does what — create and configure your agents.</p>
        </div>
        {!creating && (
          <button className="btn btn-primary" onClick={() => setCreating(true)}>+ New agent</button>
        )}
      </div>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {creating && (
        <div className="card" style={{ marginBottom: 16 }}>
          <h3 style={{ marginTop: 0 }}>New agent</h3>
          <div className="cfg-row">
            <span className="cfg-label">Name</span>
            <input value={form.name} onChange={e => setName(e.target.value)} placeholder="e.g. Sebastian" />
          </div>
          <div className="cfg-row">
            <span className="cfg-label">ID (slug)</span>
            <input
              value={form.id}
              onChange={e => { setIdEdited(true); setForm(f => ({ ...f, id: slugify(e.target.value) })); }}
              placeholder="sebas"
              style={{ fontFamily: 'monospace' }}
            />
          </div>
          <div className="cfg-row">
            <span className="cfg-label">Role</span>
            <input value={form.role} onChange={e => setForm(f => ({ ...f, role: e.target.value }))} placeholder="Chief of staff" />
          </div>
          <div className="cfg-row">
            <span className="cfg-label">Model tier</span>
            <select value={form.model_tier} onChange={e => setForm(f => ({ ...f, model_tier: e.target.value }))}>
              <option value="fast">fast</option>
              <option value="balanced">balanced</option>
              <option value="smart">smart</option>
            </select>
          </div>
          <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: '4px 0 10px' }}>
            You'll set the persona (soul, operating notes, user context) on the next screen — or draft it with AI.
          </p>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn btn-primary" disabled={!canCreate || saving} onClick={create}>
              {saving ? 'Creating…' : 'Create & configure'}
            </button>
            <button className="btn" disabled={saving} onClick={() => { setCreating(false); setError(null); }}>Cancel</button>
          </div>
        </div>
      )}

      <div className="grid">
        {agents.map(a => {
          const caps = Array.isArray(a.capabilities) ? a.capabilities : [];
          return (
            <Link
              key={a.id}
              to={`/agents/${a.id}`}
              className="card agent-card"
              data-role={a.role}
              style={{ textDecoration: 'none', color: 'inherit' }}
            >
              <h3 style={{ marginBottom: 4, wordBreak: 'break-word' }}>{a.name}</h3>
              <p className="agent-role" style={{ marginTop: 0, wordBreak: 'break-word' }}>{a.role}</p>
              {caps.length > 0 && (
                <div style={{ marginTop: 8, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  {caps.map(c => (
                    <span key={c} className="badge badge-type">{c}</span>
                  ))}
                </div>
              )}
              <div style={{ marginTop: 12, fontSize: 12, color: 'var(--text-muted)' }}>
                {a.model_tier && <>tier: <strong>{a.model_tier}</strong></>}
                {a.active === false && <span style={{ marginLeft: 8, color: 'var(--danger)' }}>inactive</span>}
              </div>
            </Link>
          );
        })}
        {agents.length === 0 && <p className="empty">No agents configured.</p>}
      </div>
    </div>
  );
}
