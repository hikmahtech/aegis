import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

// Ingestion channels (email / rss / raindrop / wearable) plus named places
// (`place`, B5 — reference data, not a source). DB-owned: the seed
// yaml only plants starter rows on first boot — everything here survives
// restarts. This list also drives which kinds are RENDERED, so a kind missing
// here is a channel row nobody can see or edit.

const CHANNEL_KINDS = ['email', 'rss', 'raindrop', 'wearable', 'place'] as const;
type ChannelKind = (typeof CHANNEL_KINDS)[number];

const KIND_COLORS: Record<string, string> = {
  email: 'var(--info)',
  rss: 'var(--warning)',
  raindrop: 'var(--purple)',
  wearable: 'var(--success)',
  place: 'var(--info)',
};

const KIND_HELP: Record<ChannelKind, string> = {
  email: 'Gmail accounts polled by GmailIngestFlow. The account must be authorized via the Google accounts re-auth flow before ingestion works.',
  rss: 'Feed URLs polled by RssIngestFlow.',
  raindrop: 'Raindrop.io bookmark collections (the token lives in Integrations).',
  wearable: 'Wearable vendors polled by WearableIngestFlow into life.observations. Identifier is the vendor slug (currently only "oura"); the token lives in Integrations.',
  place: 'Named places (home / office / gym) that POST /api/webhooks/life/location resolves a phone push against. Identifier is the name that gets stored; the centre coordinate below is the ONLY location AEGIS keeps — the pushed lat/lon is used to pick a place and then discarded, never written to the database or a log.',
};

// `place` is reference data, not an ingest source, so it is the one kind whose
// config carries coordinates. Without these three fields a place row is
// unusable (list_places skips it) — which is why the form renders them rather
// than leaving the admin to hand-edit JSON.
const DEFAULT_RADIUS_M = 150;

interface ChannelForm {
  kind: ChannelKind;
  identifier: string;
  label: string;
  token_path: string;
  agent_id: string;
  lat: string;
  lon: string;
  radius_m: string;
  active: boolean;
}

const emptyForm: ChannelForm = {
  kind: 'email',
  identifier: '',
  label: '',
  token_path: '',
  agent_id: '',
  lat: '',
  lon: '',
  radius_m: String(DEFAULT_RADIUS_M),
  active: true,
};

export default function Channels() {
  const [channels, setChannels] = useState<any[]>([]);
  const [agents, setAgents] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<any | null>(null);
  const [form, setForm] = useState<ChannelForm>({ ...emptyForm });
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);

  const load = () => {
    setLoading(true);
    api.listChannels()
      .then(r => { setChannels(r || []); setLoading(false); })
      .catch(e => { setError(e); setLoading(false); });
  };

  useEffect(() => {
    load();
    api.listAgents().then(setAgents).catch(() => setAgents([]));
  }, []);

  const openCreate = (kind: ChannelKind) => {
    setEditing(null);
    setForm({
      ...emptyForm,
      kind,
      identifier: kind === 'raindrop' ? 'default' : '',
    });
    setFormError('');
    setShowForm(true);
  };

  const openEdit = (c: any) => {
    const cfg = c.config || {};
    setEditing(c);
    setForm({
      kind: c.kind,
      identifier: c.identifier || '',
      label: cfg.label || '',
      token_path: cfg.token_path || '',
      agent_id: cfg.agent_id || '',
      lat: cfg.lat === undefined || cfg.lat === null ? '' : String(cfg.lat),
      lon: cfg.lon === undefined || cfg.lon === null ? '' : String(cfg.lon),
      radius_m: cfg.radius_m === undefined || cfg.radius_m === null ? String(DEFAULT_RADIUS_M) : String(cfg.radius_m),
      active: !!c.active,
    });
    setFormError('');
    setShowForm(true);
  };

  const buildConfig = (): any => {
    // Preserve unknown config keys (e.g. last_cursor) when editing.
    const base = editing ? { ...(editing.config || {}) } : {};
    if (form.kind === 'email') {
      base.label = form.label.trim();
      base.token_path = form.token_path.trim()
        || `config/credentials/${form.label.trim() || 'primary'}.json`;
    } else if (form.kind === 'rss') {
      base.label = form.label.trim();
      if (base.last_cursor === undefined) base.last_cursor = null;
    } else if (form.kind === 'place') {
      base.label = form.label.trim();
      base.lat = Number(form.lat);
      base.lon = Number(form.lon);
      base.radius_m = Number(form.radius_m) || DEFAULT_RADIUS_M;
    } else {
      if (base.last_cursor === undefined) base.last_cursor = null;
    }
    if (form.agent_id) base.agent_id = form.agent_id;
    else delete base.agent_id;
    return base;
  };

  const handleSave = async () => {
    if (!form.identifier.trim()) {
      setFormError(form.kind === 'rss' ? 'Feed URL is required' : 'Identifier is required');
      return;
    }
    if (form.kind === 'place') {
      const lat = Number(form.lat);
      const lon = Number(form.lon);
      if (!form.lat.trim() || !form.lon.trim() || Number.isNaN(lat) || Number.isNaN(lon)
          || lat < -90 || lat > 90 || lon < -180 || lon > 180) {
        setFormError('Latitude (-90..90) and longitude (-180..180) are required for a place');
        return;
      }
      if (!(Number(form.radius_m) > 0)) {
        setFormError('Radius must be a positive number of metres');
        return;
      }
    }
    setSaving(true);
    setFormError('');
    try {
      if (editing) {
        await api.updateChannel(editing.id, {
          identifier: form.identifier.trim(),
          config: buildConfig(),
          active: form.active,
        });
      } else {
        await api.createChannel({
          kind: form.kind,
          identifier: form.identifier.trim(),
          config: buildConfig(),
          active: form.active,
        });
      }
      setShowForm(false);
      setEditing(null);
      load();
    } catch (err: any) {
      setFormError(err.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const toggleActive = async (c: any) => {
    try {
      await api.updateChannel(c.id, { active: !c.active });
      setChannels(prev => prev.map(x => (x.id === c.id ? { ...x, active: !c.active } : x)));
    } catch (err: any) {
      setError(err);
    }
  };

  const handleDelete = async (c: any) => {
    if (!confirm(`Delete ${c.kind} channel "${c.identifier}"? Ingestion for it stops immediately.`)) return;
    try {
      await api.deleteChannel(c.id);
      load();
    } catch (err: any) {
      setError(err);
    }
  };

  const byKind = (kind: string) => channels.filter(c => c.kind === kind);
  const agentName = (id: string) => agents.find(a => a.id === id)?.name || id;

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">Channels</h1>
          <p className="page-subtitle">
            Ingestion sources (email / RSS / Raindrop / wearable) and named places.
            Managed here — the seed yaml only
            plants starter examples on first boot; edits and additions survive restarts.
          </p>
        </div>
      </div>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {showForm && (
        <div className="modal-overlay" onClick={() => setShowForm(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{editing ? `Edit ${form.kind} channel` : `New ${form.kind} channel`}</h3>
              <button className="modal-close" onClick={() => setShowForm(false)}>&times;</button>
            </div>
            <div className="modal-body">
              {formError && <div className="form-error">{formError}</div>}
              <div className="form-group">
                <label>{form.kind === 'email' ? 'Email address' : form.kind === 'rss' ? 'Feed URL' : form.kind === 'place' ? 'Place name' : 'Identifier'}</label>
                <input
                  value={form.identifier}
                  onChange={e => setForm({ ...form, identifier: e.target.value })}
                  placeholder={form.kind === 'email' ? 'you@example.com' : form.kind === 'rss' ? 'https://example.com/feed.xml' : form.kind === 'place' ? 'home' : 'default'}
                  className="mono"
                />
              </div>
              {form.kind !== 'raindrop' && (
                <div className="form-group">
                  <label>Label</label>
                  <input
                    value={form.label}
                    onChange={e => setForm({ ...form, label: e.target.value })}
                    placeholder={form.kind === 'email' ? 'primary' : 'hn-frontpage'}
                  />
                </div>
              )}
              {form.kind === 'email' && (
                <div className="form-group">
                  <label>Token path</label>
                  <input
                    value={form.token_path}
                    onChange={e => setForm({ ...form, token_path: e.target.value })}
                    placeholder="config/credentials/<label>.json (default)"
                    className="mono"
                  />
                  <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: '4px 0 0' }}>
                    The account must be authorized via the Google accounts re-auth flow on
                    the <Link to="/flows">Flows page</Link> (use the same label) — it writes
                    the OAuth token to this path.
                  </p>
                </div>
              )}
              {form.kind === 'place' && (
                <>
                  <div className="form-group">
                    <label>Centre latitude</label>
                    <input
                      value={form.lat}
                      onChange={e => setForm({ ...form, lat: e.target.value })}
                      placeholder="19.0760"
                      className="mono"
                    />
                  </div>
                  <div className="form-group">
                    <label>Centre longitude</label>
                    <input
                      value={form.lon}
                      onChange={e => setForm({ ...form, lon: e.target.value })}
                      placeholder="72.8777"
                      className="mono"
                    />
                  </div>
                  <div className="form-group">
                    <label>Radius (metres)</label>
                    <input
                      value={form.radius_m}
                      onChange={e => setForm({ ...form, radius_m: e.target.value })}
                      placeholder={String(DEFAULT_RADIUS_M)}
                      className="mono"
                    />
                    <p style={{ fontSize: 12, color: 'var(--text-muted)', margin: '4px 0 0' }}>
                      A push inside this circle resolves to this place; overlapping
                      circles resolve to the smallest one. Outside every place, the
                      stored label is "elsewhere".
                    </p>
                  </div>
                </>
              )}
              <div className="form-group">
                <label>Agent</label>
                <select value={form.agent_id} onChange={e => setForm({ ...form, agent_id: e.target.value })}>
                  <option value="">— none —</option>
                  {agents.filter(a => a.id !== 'system').map(a => (
                    <option key={a.id} value={a.id}>{a.name} ({a.id})</option>
                  ))}
                </select>
              </div>
              <div className="form-group">
                <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={form.active}
                    onChange={e => setForm({ ...form, active: e.target.checked })}
                    style={{ width: 'auto' }}
                  />
                  Active
                </label>
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn" onClick={() => setShowForm(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
                {saving ? 'Saving...' : editing ? 'Update' : 'Create'}
              </button>
            </div>
          </div>
        </div>
      )}

      {loading ? (
        <div className="loading">Loading channels...</div>
      ) : (
        CHANNEL_KINDS.map(kind => {
          const items = byKind(kind);
          return (
            <div key={kind} className="section">
              <div className="page-header-row" style={{ marginBottom: 8 }}>
                <h2 className="section-title" style={{ color: KIND_COLORS[kind] || 'inherit', marginBottom: 0 }}>
                  {kind}
                  <span className="count-badge">{items.length}</span>
                </h2>
                <button className="btn" onClick={() => openCreate(kind)}>+ Add {kind}</button>
              </div>
              <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 0 }}>{KIND_HELP[kind]}</p>
              {items.length === 0 ? (
                <div className="empty">No {kind} channels</div>
              ) : (
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Identifier</th>
                        <th>Label</th>
                        <th>Agent</th>
                        <th>Active</th>
                        <th style={{ width: 90 }} />
                      </tr>
                    </thead>
                    <tbody>
                      {items.map(c => (
                        <tr key={c.id}>
                          <td className="mono" style={{ wordBreak: 'break-all' }}>{c.identifier}</td>
                          <td>{c.config?.label || '—'}</td>
                          <td>{c.config?.agent_id ? agentName(c.config.agent_id) : '—'}</td>
                          <td>
                            <label className="toggle-switch" title={c.active ? 'Deactivate' : 'Activate'}>
                              <input type="checkbox" checked={!!c.active} onChange={() => toggleActive(c)} />
                              <span className="toggle-slider" />
                            </label>
                          </td>
                          <td>
                            <button className="btn-icon" title="Edit" onClick={() => openEdit(c)}>&#9998;</button>
                            <button className="btn-icon btn-icon-danger" title="Delete" onClick={() => handleDelete(c)}>&times;</button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}
