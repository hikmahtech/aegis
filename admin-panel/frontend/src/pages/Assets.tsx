import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface AssetFormData {
  name: string;
  kind: string;
  purchase_date: string;
  warranty_until: string;
  service_interval_days: string;
  last_serviced_at: string;
  location: string;
  notes: string;
}

const emptyForm: AssetFormData = {
  name: '',
  kind: '',
  purchase_date: '',
  warranty_until: '',
  service_interval_days: '',
  last_serviced_at: '',
  location: '',
  notes: '',
};

// Suggestions only — `kind` is an open string server-side (no enum), so a new
// kind of thing never needs a code change.
const KIND_SUGGESTIONS = [
  'car', 'appliance', 'hvac', 'plumbing', 'electronics', 'furniture', 'tool',
];

const asDate = (v: string | null) => (v ? String(v).slice(0, 10) : '');

// Mirrors services/assets.service_due_on: both inputs required, non-positive
// interval means "no service schedule".
const serviceDueOn = (a: any): string => {
  const days = Number(a.service_interval_days);
  const last = asDate(a.last_serviced_at);
  if (!last || !days || days <= 0) return '';
  const d = new Date(`${last}T00:00:00`);
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
};

export default function Assets() {
  const [assets, setAssets] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [kindFilter, setKindFilter] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<AssetFormData>({ ...emptyForm });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = (kind?: string) => {
    setLoading(true);
    api.listAssets(kind || undefined)
      .then(r => { setAssets(r || []); setLoading(false); })
      .catch(() => { setAssets([]); setLoading(false); });
  };

  useEffect(() => { load(); }, []);

  const openCreate = () => {
    setEditingId(null);
    setForm({ ...emptyForm });
    setShowForm(true);
    setError('');
  };

  const openEdit = (a: any) => {
    setEditingId(a.id);
    setForm({
      name: a.name || '',
      kind: a.kind || '',
      purchase_date: asDate(a.purchase_date),
      warranty_until: asDate(a.warranty_until),
      service_interval_days: a.service_interval_days == null ? '' : String(a.service_interval_days),
      last_serviced_at: asDate(a.last_serviced_at),
      location: a.location || '',
      notes: a.notes || '',
    });
    setShowForm(true);
    setError('');
  };

  const handleSave = async () => {
    if (!form.name.trim()) { setError('Name is required'); return; }
    if (!form.kind.trim()) { setError('Kind is required'); return; }
    setSaving(true);
    setError('');
    try {
      const interval = parseInt(form.service_interval_days, 10);
      const payload: any = {
        name: form.name.trim(),
        kind: form.kind.trim(),
        purchase_date: form.purchase_date || null,
        warranty_until: form.warranty_until || null,
        // Explicit null clears the field server-side, which is how a service
        // reminder gets switched off.
        service_interval_days: isNaN(interval) ? null : interval,
        last_serviced_at: form.last_serviced_at || null,
        location: form.location,
        notes: form.notes,
      };
      if (editingId) {
        await api.updateAsset(editingId, payload);
      } else {
        await api.createAsset(payload);
      }
      setShowForm(false);
      setEditingId(null);
      load(kindFilter);
    } catch (err: any) {
      setError(err.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete "${name}" from the asset registry?`)) return;
    try {
      await api.deleteAsset(id);
      load(kindFilter);
    } catch (err: any) {
      alert(err.message || 'Delete failed');
    }
  };

  const kinds = Array.from(new Set(assets.map(a => a.kind))).sort();

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">Assets</h1>
          <p className="page-subtitle">
            {assets.length} {kindFilter ? `${kindFilter} assets` : 'tracked assets'}
          </p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>+ Add Asset</button>
      </div>

      <div className="filter-bar">
        <select
          value={kindFilter}
          onChange={e => { setKindFilter(e.target.value); load(e.target.value); }}
        >
          <option value="">All kinds</option>
          {kinds.map(k => <option key={k} value={k}>{k}</option>)}
        </select>
        <span className="meta">
          Set a service interval and a last-serviced date to have the asset appear in the Expiry Radar.
        </span>
      </div>

      {showForm && (
        <div className="modal-overlay" onClick={() => setShowForm(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{editingId ? 'Edit Asset' : 'New Asset'}</h3>
              <button className="modal-close" onClick={() => setShowForm(false)}>&times;</button>
            </div>
            <div className="modal-body">
              {error && <div className="form-error">{error}</div>}
              <div className="form-row">
                <div className="form-group" style={{ flex: 2 }}>
                  <label>Name</label>
                  <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="e.g. Bosch washing machine" />
                </div>
                <div className="form-group">
                  <label>Kind</label>
                  <input list="asset-kinds" value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value })} placeholder="e.g. appliance" className="mono" />
                  <datalist id="asset-kinds">
                    {KIND_SUGGESTIONS.map(k => <option key={k} value={k} />)}
                  </datalist>
                </div>
              </div>
              <div className="form-row">
                <div className="form-group">
                  <label>Purchased on</label>
                  <input type="date" value={form.purchase_date} onChange={e => setForm({ ...form, purchase_date: e.target.value })} />
                </div>
                <div className="form-group">
                  <label>Warranty until</label>
                  <input type="date" value={form.warranty_until} onChange={e => setForm({ ...form, warranty_until: e.target.value })} />
                </div>
              </div>
              <div className="form-row">
                <div className="form-group">
                  <label>Service interval (days)</label>
                  <input value={form.service_interval_days} onChange={e => setForm({ ...form, service_interval_days: e.target.value })} placeholder="e.g. 365" className="mono" />
                </div>
                <div className="form-group">
                  <label>Last serviced</label>
                  <input type="date" value={form.last_serviced_at} onChange={e => setForm({ ...form, last_serviced_at: e.target.value })} />
                </div>
              </div>
              <p className="meta" style={{ margin: '0 0 0.75rem' }}>
                Both fields together create a "Service due" item in the Expiry Radar. Clearing either removes it.
              </p>
              <div className="form-group">
                <label>Location</label>
                <input value={form.location} onChange={e => setForm({ ...form, location: e.target.value })} placeholder="e.g. utility room" />
              </div>
              <div className="form-group">
                <label>Notes</label>
                <textarea rows={4} value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })} placeholder="Model number, dealer, service contact..." />
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn" onClick={() => setShowForm(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
                {saving ? 'Saving...' : editingId ? 'Update' : 'Create'}
              </button>
            </div>
          </div>
        </div>
      )}

      {loading ? (
        <div className="loading">Loading assets...</div>
      ) : assets.length === 0 ? (
        <div className="empty">{kindFilter ? `No ${kindFilter} assets` : 'Nothing tracked yet'}</div>
      ) : (
        <div className="resource-grid">
          {assets.map(a => {
            const isExpanded = expandedId === a.id;
            const due = serviceDueOn(a);
            return (
              <div key={a.id} className="resource-card" onClick={() => setExpandedId(isExpanded ? null : a.id)}>
                <div className="resource-card-header">
                  <span className="resource-type-label mono">{a.kind}</span>
                  <div className="resource-actions">
                    <button className="btn-icon" title="Edit" onClick={e => { e.stopPropagation(); openEdit(a); }}>&#9998;</button>
                    <button className="btn-icon btn-icon-danger" title="Delete" onClick={e => { e.stopPropagation(); handleDelete(a.id, a.name); }}>&times;</button>
                  </div>
                </div>
                <h4 className="resource-title">{a.name}</h4>
                <div className="resource-path mono">{a.slug}{a.location ? ` · ${a.location}` : ''}</div>
                <div className="resource-meta">
                  {a.warranty_until && <span className="meta-tag mono">warranty {asDate(a.warranty_until)}</span>}
                  {due && <span className="meta-tag mono">service due {due}</span>}
                </div>
                {isExpanded && a.notes && (
                  <div className="resource-content"><pre>{a.notes}</pre></div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
