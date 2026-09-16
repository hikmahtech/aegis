import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import DataTable from '../components/DataTable';

// Per-alert runbooks stored in the database (the `runbooks` table, #499).
// Pandora puts the runbook for an alert in front of every investigation of it:
// a runbook saved here wins, and the built-in runbooks/<AlertName>.md from the
// repo is the fallback. Runbooks about your own setup belong here, not in the
// repo. The alert name matches in any spelling: "NodeDown", "node-down" and
// "Node Down" are one runbook.
export default function Runbooks() {
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  // null = editor closed; editing = the name of the row being edited, or '' for new.
  const [editing, setEditing] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [body, setBody] = useState('');
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);

  async function load() {
    setError(null); setLoading(true);
    try { setRows(await api.listRunbooks()); }
    catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  function openNew() {
    setEditing(''); setName(''); setBody(''); setFormError('');
  }

  async function openEdit(rowName: string) {
    setFormError('');
    try {
      const rb = await api.getRunbook(rowName);
      setEditing(rowName); setName(rb.name); setBody(rb.body);
    } catch (e: any) { setError(e); }
  }

  async function save() {
    if (!name.trim()) { setFormError('Alert name is required'); return; }
    setSaving(true); setFormError('');
    try {
      await api.putRunbook(name.trim(), body);
      setEditing(null);
      await load();
    } catch (e: any) {
      // The API answers 400 with the reason (blank, a stub, too long).
      setFormError(e.message || 'Save failed');
    } finally { setSaving(false); }
  }

  async function remove(rowName: string) {
    if (!confirm(`Delete the stored runbook for "${rowName}"? The built-in file, if there is one, applies again.`)) return;
    try { await api.deleteRunbook(rowName); await load(); }
    catch (e: any) { setError(e); }
  }

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">Runbooks</h1>
          <p className="page-subtitle">
            An alert&apos;s runbook goes in front of every investigation of it. One saved here wins
            over the built-in <span className="mono">runbooks/&lt;AlertName&gt;.md</span>. Keep
            anything about your own machines here, not in the repo.
          </p>
        </div>
        <button className="btn btn-primary" onClick={openNew}>+ Add runbook</button>
      </div>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {editing !== null && (
        <div className="modal-overlay" onClick={() => setEditing(null)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{editing ? `Edit ${editing}` : 'New runbook'}</h3>
              <button className="modal-close" onClick={() => setEditing(null)}>&times;</button>
            </div>
            <div className="modal-body">
              {formError && <div className="form-error">{formError}</div>}
              <div className="form-group">
                <label>Alert name</label>
                <input
                  value={name}
                  onChange={e => setName(e.target.value)}
                  disabled={editing !== ''}
                  placeholder="NodeDown, Dagster Pipeline Failure, …"
                  className="mono"
                />
                <p className="meta" style={{ margin: '0.25rem 0 0' }}>
                  The alertname or rule title. Case and punctuation (spaces, hyphens, underscores) are ignored.
                </p>
              </div>
              <div className="form-group">
                <label>Runbook (Markdown)</label>
                <textarea
                  rows={18}
                  value={body}
                  onChange={e => setBody(e.target.value)}
                  className="mono"
                  style={{ width: '100%', fontSize: 12 }}
                  placeholder={'# NodeDown\n\n## First three checks\n1. …'}
                />
                <p className="meta" style={{ margin: '0.25rem 0 0' }}>{body.trim().length} characters</p>
              </div>
            </div>
            <div className="modal-footer">
              <button className="btn" onClick={() => setEditing(null)}>Cancel</button>
              <button className="btn btn-primary" onClick={() => void save()} disabled={saving}>
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}

      {loading && <div className="loading">Loading runbooks…</div>}
      <div className="table-scroll">
        <DataTable
          rows={rows}
          rowKey={r => r.name_key}
          emptyText={loading ? undefined : 'No stored runbooks. Investigations use the built-in files.'}
          columns={[
            { header: 'Alert name', td: { className: 'mono' }, cell: r => r.name },
            { header: 'Characters', th: { style: { width: 110 } }, cell: r => r.chars },
            {
              header: 'Updated',
              th: { style: { width: 200 } },
              td: { className: 'meta' },
              cell: r => (r.updated_at ? new Date(r.updated_at).toLocaleString() : '—'),
            },
            {
              header: 'By',
              th: { style: { width: 160 } },
              td: { className: 'meta' },
              cell: r => r.updated_by || '—',
            },
            {
              th: { style: { width: 140 } },
              cell: r => (
                <>
                  <button className="btn btn-sm" onClick={() => void openEdit(r.name)}>Edit</button>{' '}
                  <button className="btn btn-sm" onClick={() => void remove(r.name)}>Delete</button>
                </>
              ),
            },
          ]}
        />
      </div>
    </div>
  );
}
