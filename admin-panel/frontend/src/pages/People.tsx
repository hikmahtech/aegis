import { useEffect, useState } from 'react';
import { api } from '../api/client';

interface PersonFormData {
  name: string;
  aliases: string;
  relationship: string;
  key_dates: string;
  notes: string;
  last_contact: string;
}

const emptyForm: PersonFormData = {
  name: '',
  aliases: '',
  relationship: '',
  key_dates: '{}',
  notes: '',
  last_contact: '',
};

// "2026-07-31T09:15:00+00:00" -> "2026-07-31" for display.
const asDate = (v: string | null) => (v ? String(v).slice(0, 10) : '');

export default function People() {
  const [people, setPeople] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<PersonFormData>({ ...emptyForm });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = (q?: string) => {
    setLoading(true);
    api.listPeople(q).then(r => {
      setPeople(r || []);
      setLoading(false);
    }).catch(() => { setPeople([]); setLoading(false); });
  };

  useEffect(() => { load(); }, []);

  const openCreate = () => {
    setEditingId(null);
    setForm({ ...emptyForm });
    setShowForm(true);
    setError('');
  };

  const openEdit = (p: any) => {
    setEditingId(p.id);
    setForm({
      name: p.name || '',
      aliases: (p.aliases || []).join(', '),
      relationship: p.relationship || '',
      key_dates: JSON.stringify(p.key_dates || {}, null, 2),
      notes: p.notes || '',
      last_contact: asDate(p.last_contact),
    });
    setShowForm(true);
    setError('');
  };

  const handleSave = async () => {
    if (!form.name.trim()) { setError('Name is required'); return; }
    let keyDates: Record<string, any> = {};
    try { keyDates = JSON.parse(form.key_dates || '{}'); } catch { setError('Invalid JSON in key dates'); return; }
    setSaving(true);
    setError('');
    try {
      const payload = {
        name: form.name.trim(),
        aliases: form.aliases.split(',').map(a => a.trim()).filter(Boolean),
        relationship: form.relationship.trim(),
        key_dates: keyDates,
        notes: form.notes,
        last_contact: form.last_contact ? `${form.last_contact}T00:00:00Z` : null,
      };
      if (editingId) {
        await api.updatePerson(editingId, payload);
      } else {
        await api.createPerson(payload);
      }
      setShowForm(false);
      setEditingId(null);
      load(search || undefined);
    } catch (err: any) {
      setError(err.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete "${name}" from the people registry?`)) return;
    try {
      await api.deletePerson(id);
      load(search || undefined);
    } catch (err: any) {
      alert(err.message || 'Delete failed');
    }
  };

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">People</h1>
          <p className="page-subtitle">{people.length} {search ? 'matching' : 'people'}</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>+ Add Person</button>
      </div>

      <div className="filter-bar">
        <input
          value={search}
          placeholder="Exact name or alias (e.g. an email address)"
          onChange={e => setSearch(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') load(search || undefined); }}
          style={{ maxWidth: 360 }}
        />
        <button className="btn" onClick={() => load(search || undefined)}>Search</button>
        {search && <button className="btn" onClick={() => { setSearch(''); load(); }}>Clear</button>}
      </div>

      {showForm && (
        <div className="modal-overlay" onClick={() => setShowForm(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{editingId ? 'Edit Person' : 'New Person'}</h3>
              <button className="modal-close" onClick={() => setShowForm(false)}>&times;</button>
            </div>
            <div className="modal-body">
              {error && <div className="form-error">{error}</div>}
              <div className="form-row">
                <div className="form-group" style={{ flex: 2 }}>
                  <label>Name</label>
                  <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="e.g. Sajid Ansari" />
                </div>
                <div className="form-group">
                  <label>Relationship</label>
                  <input value={form.relationship} onChange={e => setForm({ ...form, relationship: e.target.value })} placeholder="e.g. brother, colleague, GP" />
                </div>
              </div>
              <div className="form-group">
                <label>Aliases (comma-separated)</label>
                <input value={form.aliases} onChange={e => setForm({ ...form, aliases: e.target.value })} placeholder="nickname, someone@example.com" className="mono" />
                <p className="meta" style={{ margin: '0.25rem 0 0' }}>Nicknames, other names, and email addresses. Stored lowercased; search matches any of them.</p>
              </div>
              <div className="form-group">
                <label>Key dates (JSON)</label>
                <textarea rows={3} value={form.key_dates} onChange={e => setForm({ ...form, key_dates: e.target.value })} className="mono" placeholder='{"birthday": "1985-04-02"}' />
                <p className="meta" style={{ margin: '0.25rem 0 0' }}>ISO dates. Use <span className="mono">--MM-DD</span> when the year is unknown.</p>
              </div>
              <div className="form-group">
                <label>Last contact</label>
                <input type="date" value={form.last_contact} onChange={e => setForm({ ...form, last_contact: e.target.value })} />
              </div>
              <div className="form-group">
                <label>Notes</label>
                <textarea rows={4} value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })} placeholder="Context worth remembering..." />
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
        <div className="loading">Loading people...</div>
      ) : people.length === 0 ? (
        <div className="empty">{search ? 'No person matches that name or alias' : 'No people yet'}</div>
      ) : (
        <div className="resource-grid">
          {people.map(p => {
            const isExpanded = expandedId === p.id;
            const dates = p.key_dates || {};
            return (
              <div key={p.id} className="resource-card" onClick={() => setExpandedId(isExpanded ? null : p.id)}>
                <div className="resource-card-header">
                  <span className="resource-type-label mono">{p.relationship || 'person'}</span>
                  <div className="resource-actions">
                    <button className="btn-icon" title="Edit" onClick={e => { e.stopPropagation(); openEdit(p); }}>&#9998;</button>
                    <button className="btn-icon btn-icon-danger" title="Delete" onClick={e => { e.stopPropagation(); handleDelete(p.id, p.name); }}>&times;</button>
                  </div>
                </div>
                <h4 className="resource-title">{p.name}</h4>
                {p.last_contact && <div className="resource-path mono">last contact {asDate(p.last_contact)}</div>}
                {(p.aliases || []).length > 0 && (
                  <div className="resource-meta">
                    {p.aliases.map((a: string) => <span key={a} className="meta-tag mono">{a}</span>)}
                  </div>
                )}
                {Object.keys(dates).length > 0 && (
                  <div className="resource-meta">
                    {Object.entries(dates).map(([k, v]) => (
                      <span key={k} className="meta-tag">{k}: {String(v)}</span>
                    ))}
                  </div>
                )}
                {isExpanded && p.notes && (
                  <div className="resource-content"><pre>{p.notes}</pre></div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
