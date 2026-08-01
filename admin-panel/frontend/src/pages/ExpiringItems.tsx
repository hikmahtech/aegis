import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface ItemFormData {
  kind: string;
  title: string;
  expires_on: string;
  lead_days: string;
  person_id: string;
  notes: string;
}

const emptyForm: ItemFormData = {
  kind: '',
  title: '',
  expires_on: '',
  lead_days: '30, 7, 1',
  person_id: '',
  notes: '',
};

// Suggestions only — `kind` is an open string server-side (no enum), so a new
// kind of document never needs a code change.
const KIND_SUGGESTIONS = [
  'passport', 'visa', 'licence', 'insurance', 'warranty', 'medication', 'domain',
];

const asDate = (v: string | null) => (v ? String(v).slice(0, 10) : '');

// Whole days from today to `expires_on` — negative once it has expired.
const daysLeft = (item: any): number => {
  if (typeof item.days_left === 'number') return item.days_left;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const due = new Date(`${asDate(item.expires_on)}T00:00:00`);
  return Math.round((due.getTime() - today.getTime()) / 86400000);
};

const urgencyLabel = (d: number) =>
  d < 0 ? `expired ${-d}d ago` : d === 0 ? 'expires today' : `${d}d left`;

export default function ExpiringItems() {
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [window, setWindow] = useState<number | ''>('');
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ItemFormData>({ ...emptyForm });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = (dueWithin?: number | '') => {
    setLoading(true);
    api.listExpiringItems(dueWithin === '' || dueWithin === undefined ? undefined : { dueWithin })
      .then(r => { setItems(r || []); setLoading(false); })
      .catch(() => { setItems([]); setLoading(false); });
  };

  useEffect(() => { load(); }, []);

  const openCreate = () => {
    setEditingId(null);
    setForm({ ...emptyForm });
    setShowForm(true);
    setError('');
  };

  const openEdit = (it: any) => {
    setEditingId(it.id);
    setForm({
      kind: it.kind || '',
      title: it.title || '',
      expires_on: asDate(it.expires_on),
      lead_days: (it.lead_days || []).join(', '),
      person_id: it.person_id || '',
      notes: it.notes || '',
    });
    setShowForm(true);
    setError('');
  };

  const handleSave = async () => {
    if (!form.kind.trim()) { setError('Kind is required'); return; }
    if (!form.title.trim()) { setError('Title is required'); return; }
    if (!form.expires_on) { setError('Expiry date is required'); return; }
    setSaving(true);
    setError('');
    try {
      const payload: any = {
        kind: form.kind.trim(),
        title: form.title.trim(),
        expires_on: form.expires_on,
        lead_days: form.lead_days.split(',').map(d => parseInt(d.trim(), 10)).filter(d => !isNaN(d)),
        person_id: form.person_id.trim() || null,
        notes: form.notes,
      };
      if (editingId) {
        await api.updateExpiringItem(editingId, payload);
      } else {
        await api.createExpiringItem(payload);
      }
      setShowForm(false);
      setEditingId(null);
      load(window);
    } catch (err: any) {
      setError(err.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string, title: string) => {
    if (!confirm(`Delete "${title}" from the expiry registry?`)) return;
    try {
      await api.deleteExpiringItem(id);
      load(window);
    } catch (err: any) {
      alert(err.message || 'Delete failed');
    }
  };

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">Expiry Radar</h1>
          <p className="page-subtitle">
            {items.length} {window === '' ? 'tracked items' : `due within ${window} days`}
          </p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>+ Add Item</button>
      </div>

      <div className="filter-bar">
        <select
          value={window}
          onChange={e => {
            const v = e.target.value === '' ? '' : parseInt(e.target.value, 10);
            setWindow(v);
            load(v);
          }}
        >
          <option value="">All items</option>
          <option value="7">Due within 7 days</option>
          <option value="30">Due within 30 days</option>
          <option value="90">Due within 90 days</option>
          <option value="365">Due within a year</option>
        </select>
        <span className="meta">Windowed views include already-expired items.</span>
      </div>

      {showForm && (
        <div className="modal-overlay" onClick={() => setShowForm(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{editingId ? 'Edit Item' : 'New Item'}</h3>
              <button className="modal-close" onClick={() => setShowForm(false)}>&times;</button>
            </div>
            <div className="modal-body">
              {error && <div className="form-error">{error}</div>}
              <div className="form-row">
                <div className="form-group" style={{ flex: 2 }}>
                  <label>Title</label>
                  <input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="e.g. Indian passport (Arshad)" />
                </div>
                <div className="form-group">
                  <label>Kind</label>
                  <input list="expiring-item-kinds" value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value })} placeholder="e.g. passport" className="mono" />
                  <datalist id="expiring-item-kinds">
                    {KIND_SUGGESTIONS.map(k => <option key={k} value={k} />)}
                  </datalist>
                </div>
              </div>
              <div className="form-row">
                <div className="form-group">
                  <label>Expires on</label>
                  <input type="date" value={form.expires_on} onChange={e => setForm({ ...form, expires_on: e.target.value })} />
                </div>
                <div className="form-group">
                  <label>Lead days (comma-separated)</label>
                  <input value={form.lead_days} onChange={e => setForm({ ...form, lead_days: e.target.value })} placeholder="30, 7, 1" className="mono" />
                </div>
              </div>
              <div className="form-group">
                <label>Person ID (optional)</label>
                <input value={form.person_id} onChange={e => setForm({ ...form, person_id: e.target.value })} placeholder="uuid from the People page" className="mono" />
                <p className="meta" style={{ margin: '0.25rem 0 0' }}>Links this document to someone in the people registry. Cleared automatically if that person is deleted.</p>
              </div>
              <div className="form-group">
                <label>Notes</label>
                <textarea rows={4} value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })} placeholder="Renewal office, reference number, what it needs..." />
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
        <div className="loading">Loading expiring items...</div>
      ) : items.length === 0 ? (
        <div className="empty">{window === '' ? 'Nothing tracked yet' : `Nothing due within ${window} days`}</div>
      ) : (
        <div className="resource-grid">
          {items.map(it => {
            const isExpanded = expandedId === it.id;
            const d = daysLeft(it);
            return (
              <div key={it.id} className="resource-card" onClick={() => setExpandedId(isExpanded ? null : it.id)}>
                <div className="resource-card-header">
                  <span className="resource-type-label mono">{it.kind}</span>
                  <div className="resource-actions">
                    <button className="btn-icon" title="Edit" onClick={e => { e.stopPropagation(); openEdit(it); }}>&#9998;</button>
                    <button className="btn-icon btn-icon-danger" title="Delete" onClick={e => { e.stopPropagation(); handleDelete(it.id, it.title); }}>&times;</button>
                  </div>
                </div>
                <h4 className="resource-title">{it.title}</h4>
                <div className="resource-path mono">{asDate(it.expires_on)} &middot; {urgencyLabel(d)}</div>
                {(it.lead_days || []).length > 0 && (
                  <div className="resource-meta">
                    {it.lead_days.map((l: number) => <span key={l} className="meta-tag mono">warn {l}d</span>)}
                  </div>
                )}
                {isExpanded && it.notes && (
                  <div className="resource-content"><pre>{it.notes}</pre></div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
