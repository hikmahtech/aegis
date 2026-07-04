import { useEffect, useState } from 'react';
import { api } from '../api/client';

const RESOURCE_KINDS = ['connector', 'runbook', 'endpoint', 'mcp_server', 'repository'];

const KIND_COLORS: Record<string, string> = {
  connector: 'var(--info)',
  runbook: 'var(--warning)',
  endpoint: 'var(--success)',
  mcp_server: 'var(--purple)',
  repository: 'var(--orange)',
};

interface ResourceFormData {
  kind: string;
  slug: string;
  title: string;
  url: string;
  content: string;
  tags: string;
  workspace_path: string;
  github_repo: string;
  coding_enabled: boolean;
  engine: string;
  claude_account: string;
  sentry_project: string;
  metadata: string;
  infra_id: string;
}

const emptyForm: ResourceFormData = {
  kind: 'repository',
  slug: '',
  title: '',
  url: '',
  content: '',
  tags: '',
  workspace_path: '',
  github_repo: '',
  coding_enabled: false,
  engine: '',
  claude_account: '',
  sentry_project: '',
  metadata: '{}',
  infra_id: '',
};

export default function Resources() {
  const [resources, setResources] = useState<any[]>([]);
  const [infra, setInfra] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterKind, setFilterKind] = useState<string>('');
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<ResourceFormData>({ ...emptyForm });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api.listResources().catch(() => []).then(r => {
      setResources(r || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  };

  useEffect(() => {
    load();
    api.listInfra().then(setInfra).catch(() => setInfra([]));
  }, []);

  const filtered = filterKind ? resources.filter(r => r.kind === filterKind) : resources;

  const grouped: Record<string, any[]> = {};
  for (const r of filtered) {
    const key = r.kind || 'other';
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(r);
  }

  const openCreate = () => {
    setEditingId(null);
    setForm({ ...emptyForm, kind: filterKind || 'repository' });
    setShowForm(true);
    setError('');
  };

  const openEdit = (r: any) => {
    setEditingId(r.id);
    // path + github_repo are edited via their own fields; keep the rest in the
    // raw "additional metadata" box so both aren't editable in two places.
    const { path, github_repo, coding_enabled, engine, claude_account, sentry_project, ...restMeta } = r.metadata || {};
    setForm({
      kind: r.kind || 'repository',
      slug: r.slug || '',
      title: r.title || '',
      url: r.url || '',
      content: r.content || '',
      tags: (r.tags || []).join(', '),
      workspace_path: path || '',
      github_repo: github_repo || '',
      coding_enabled: coding_enabled === true || coding_enabled === 'true',
      engine: engine || '',
      claude_account: claude_account || '',
      sentry_project: sentry_project || '',
      metadata: JSON.stringify(restMeta, null, 2),
      infra_id: r.infra_id || '',
    });
    setShowForm(true);
    setError('');
  };

  const handleSave = async () => {
    if (!form.title.trim()) { setError('Title is required'); return; }
    if (!form.slug.trim()) { setError('Slug is required'); return; }
    let meta: Record<string, any> = {};
    let tags: string[] = [];
    try { meta = JSON.parse(form.metadata || '{}'); } catch { setError('Invalid JSON in metadata'); return; }
    // Merge the first-class coding-agent fields back into metadata.
    if (form.workspace_path.trim()) meta.path = form.workspace_path.trim();
    if (form.github_repo.trim()) meta.github_repo = form.github_repo.trim();
    if (form.kind === 'repository') {
      meta.coding_enabled = form.coding_enabled;  // allow-list gate for alert/sentry investigation
      if (form.engine) meta.engine = form.engine; else delete meta.engine;
      if (form.claude_account.trim()) meta.claude_account = form.claude_account.trim(); else delete meta.claude_account;
      if (form.sentry_project.trim()) meta.sentry_project = form.sentry_project.trim(); else delete meta.sentry_project;
    }
    tags = form.tags.split(',').map(t => t.trim()).filter(Boolean);
    setSaving(true);
    setError('');
    try {
      const { workspace_path, github_repo, coding_enabled, engine, claude_account, sentry_project, ...rest } = form;
      const payload = { ...rest, tags, metadata: meta, infra_id: form.infra_id || null };
      if (editingId) {
        await api.updateResource(editingId, payload);
      } else {
        await api.createResource(payload);
      }
      setShowForm(false);
      setEditingId(null);
      load();
    } catch (err: any) {
      setError(err.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string, title: string) => {
    if (!confirm(`Delete resource "${title}"?`)) return;
    try {
      await api.deleteResource(id);
      load();
    } catch (err: any) {
      alert(err.message || 'Delete failed');
    }
  };

  const uniqueKinds = [...new Set(resources.map(r => r.kind))].sort();

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">Resources</h1>
          <p className="page-subtitle">{resources.length} resources</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>+ Add Resource</button>
      </div>

      <div className="filter-bar">
        <button onClick={() => setFilterKind('')} className={`btn ${!filterKind ? 'active' : ''}`}>
          All
        </button>
        {uniqueKinds.map(k => (
          <button key={k} onClick={() => setFilterKind(k)} className={`btn ${filterKind === k ? 'active' : ''}`}>
            {k} ({resources.filter(r => r.kind === k).length})
          </button>
        ))}
      </div>

      {showForm && (
        <div className="modal-overlay" onClick={() => setShowForm(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{editingId ? 'Edit Resource' : 'New Resource'}</h3>
              <button className="modal-close" onClick={() => setShowForm(false)}>&times;</button>
            </div>
            <div className="modal-body">
              {error && <div className="form-error">{error}</div>}
              <div className="form-row">
                <div className="form-group">
                  <label>Kind</label>
                  <select value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value })} disabled={!!editingId}>
                    {RESOURCE_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
                  </select>
                </div>
                <div className="form-group" style={{ flex: 2 }}>
                  <label>Slug</label>
                  <input value={form.slug} onChange={e => setForm({ ...form, slug: e.target.value })} placeholder="e.g. repo-aegis" disabled={!!editingId} className="mono" />
                </div>
              </div>
              <div className="form-group">
                <label>Title</label>
                <input value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="e.g. AEGIS monorepo" />
              </div>
              <div className="form-group">
                <label>URL</label>
                <input value={form.url} onChange={e => setForm({ ...form, url: e.target.value })} placeholder="https://github.com/..." />
              </div>
              <div className="form-group">
                <label>Infrastructure (optional)</label>
                <select value={form.infra_id} onChange={e => setForm({ ...form, infra_id: e.target.value })}>
                  <option value="">— none —</option>
                  {infra.map(i => <option key={i.id} value={i.id}>{i.name} ({i.kind})</option>)}
                </select>
              </div>
              {form.kind === 'repository' && (
                <div className="form-row">
                  <div className="form-group">
                    <label>Workspace path</label>
                    <input value={form.workspace_path} onChange={e => setForm({ ...form, workspace_path: e.target.value })} placeholder="dir under repo base, e.g. aegis" className="mono" />
                    <p className="meta" style={{ margin: '0.25rem 0 0' }}>Where the coding agent (claude/kimi) checks out & runs, relative to the coding host&apos;s repo base.</p>
                  </div>
                  <div className="form-group">
                    <label>GitHub repo</label>
                    <input value={form.github_repo} onChange={e => setForm({ ...form, github_repo: e.target.value })} placeholder="owner/repo" className="mono" />
                    <p className="meta" style={{ margin: '0.25rem 0 0' }}>Drives engine routing (which org → claude/kimi) and alert-investigation repo matching.</p>
                  </div>
                </div>
              )}
              {form.kind === 'repository' && (
                <fieldset style={{ border: '1px solid var(--border, #333)', borderRadius: 6, padding: '0.5rem 0.75rem', margin: '0 0 0.75rem' }}>
                  <legend style={{ fontSize: 12, padding: '0 6px' }}>Coding-agent routing</legend>
                  <div className="form-group">
                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
                      <input type="checkbox" checked={form.coding_enabled}
                        onChange={e => setForm({ ...form, coding_enabled: e.target.checked })} />
                      Enable alert / Sentry investigation on this repo
                    </label>
                    <p className="meta" style={{ margin: '0.25rem 0 0' }}>Allow-list gate: only checked repos can trigger a coding run. Unchecked = ignored by alert investigation.</p>
                  </div>
                  <div className="form-row">
                    <div className="form-group">
                      <label>Engine override</label>
                      <select value={form.engine} onChange={e => setForm({ ...form, engine: e.target.value })}>
                        <option value="">— org routing (default) —</option>
                        <option value="claude">claude</option>
                        <option value="kimi">kimi</option>
                      </select>
                      <p className="meta" style={{ margin: '0.25rem 0 0' }}>Pin this repo&apos;s engine. Blank = decide by GitHub org.</p>
                    </div>
                    <div className="form-group">
                      <label>Claude account</label>
                      <input value={form.claude_account} onChange={e => setForm({ ...form, claude_account: e.target.value })} placeholder="config_dirs label (claude only)" className="mono" />
                      <p className="meta" style={{ margin: '0.25rem 0 0' }}>CLAUDE_CONFIG_DIR account label from the coding host&apos;s config. Ignored for kimi.</p>
                    </div>
                  </div>
                  <div className="form-group">
                    <label>Sentry project slug</label>
                    <input value={form.sentry_project} onChange={e => setForm({ ...form, sentry_project: e.target.value })} placeholder="e.g. my-app" className="mono" />
                    <p className="meta" style={{ margin: '0.25rem 0 0' }}>Maps a Sentry issue (its project slug) straight to this repo — deterministic, no LLM guess.</p>
                  </div>
                </fieldset>
              )}
              <div className="form-group">
                <label>Tags (comma-separated)</label>
                <input value={form.tags} onChange={e => setForm({ ...form, tags: e.target.value })} placeholder="aegis, python, pandoras-actor" />
              </div>
              <div className="form-group">
                <label>Content / Runbook</label>
                <textarea rows={4} value={form.content} onChange={e => setForm({ ...form, content: e.target.value })} placeholder="Runbook steps, description..." />
              </div>
              <div className="form-group">
                <label>Additional metadata (JSON)</label>
                <textarea rows={3} value={form.metadata} onChange={e => setForm({ ...form, metadata: e.target.value })} className="mono" placeholder='{"branch": "main"}' />
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
        <div className="loading">Loading resources...</div>
      ) : filtered.length === 0 ? (
        <div className="empty">No resources found</div>
      ) : (
        Object.entries(grouped).sort(([a], [b]) => a.localeCompare(b)).map(([kind, items]) => (
          <div key={kind} className="section">
            <h2 className="section-title" style={{ color: KIND_COLORS[kind] || 'inherit' }}>
              {kind}
              <span className="count-badge">{items.length}</span>
            </h2>
            <div className="resource-grid">
              {items.map(r => {
                const meta = r.metadata || {};
                const isExpanded = expandedId === r.id;
                return (
                  <div key={r.id} className="resource-card" onClick={() => setExpandedId(isExpanded ? null : r.id)}>
                    <div className="resource-card-header">
                      <span className="resource-type-dot" style={{ background: KIND_COLORS[r.kind] || 'var(--text-muted)' }} />
                      <span className="resource-type-label mono">{r.slug}</span>
                      <div className="resource-actions">
                        <button className="btn-icon" title="Edit" onClick={e => { e.stopPropagation(); openEdit(r); }}>&#9998;</button>
                        <button className="btn-icon btn-icon-danger" title="Delete" onClick={e => { e.stopPropagation(); handleDelete(r.id, r.title); }}>&times;</button>
                      </div>
                    </div>
                    <h4 className="resource-title">{r.title}</h4>
                    {r.url && <a className="resource-url" href={r.url} target="_blank" rel="noreferrer" onClick={e => e.stopPropagation()} style={{ wordBreak: 'break-word' }}>{r.url}</a>}
                    {meta.path && <div className="resource-path mono" style={{ wordBreak: 'break-word' }}>{meta.path}</div>}
                    {(r.tags || []).length > 0 && (
                      <div className="resource-meta">
                        {r.tags.map((t: string) => <span key={t} className="meta-tag">{t}</span>)}
                      </div>
                    )}
                    {isExpanded && r.content && (
                      <div className="resource-content">
                        <pre>{r.content}</pre>
                      </div>
                    )}
                    {isExpanded && Object.keys(meta).length > 0 && (
                      <div className="resource-meta" style={{ marginTop: '0.5rem' }}>
                        {Object.entries(meta).map(([k, v]) => (
                          <span key={k} className="meta-tag mono">{k}: {String(v)}</span>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))
      )}
    </div>
  );
}
