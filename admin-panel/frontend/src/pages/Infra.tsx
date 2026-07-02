import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import ActionMenu from '../components/ActionMenu';
import JsonViewer from '../components/JsonViewer';

const INFRA_KINDS = ['ssh_host', 'swarm', 'docker', 'k8s'];

interface SetupFile {
  path: string;
  content: string;
  mode: string;
}

interface InfraFormData {
  name: string;
  kind: string;
  host: string;
  ssh_user: string;
  ssh_port: string;
  ssh_key_ref: string;
  // Write-only: sent only when non-empty; server never returns the values,
  // only has_ssh_key / has_kubeconfig booleans.
  ssh_private_key: string;
  kubeconfig: string;
  docker_context: string;
  hosts_aegis: boolean;
  setup_command: string;
  setup_files: SetupFile[];
}

const emptyForm: InfraFormData = {
  name: '',
  kind: 'ssh_host',
  host: '',
  ssh_user: '',
  ssh_port: '22',
  ssh_key_ref: '',
  ssh_private_key: '',
  kubeconfig: '',
  docker_context: '',
  hosts_aegis: false,
  setup_command: '',
  setup_files: [],
};

function statusBadgeClass(status: string) {
  if (status === 'ready') return 'badge badge-success';
  if (status === 'error') return 'badge badge-error';
  return 'badge badge-neutral'; // unprovisioned, provisioning
}

// ── Legacy live service/pod inspector (collapsible, secondary section) ─────
type LiveResource = 'services' | 'pods' | 'deployments' | 'argocd';

const LIVE_DEFAULT_CONTEXT: Record<LiveResource, string> = {
  services: 'swarm',
  pods: 'acme-prod',
  deployments: 'acme-prod',
  argocd: 'acme-prod',
};

function LiveInspector() {
  const [resource, setResource] = useState<LiveResource>('services');
  const [context, setContext] = useState<string>(LIVE_DEFAULT_CONTEXT.services);
  const [namespace, setNamespace] = useState<string>('default');
  const [data, setData] = useState<any>(null);
  const [selected, setSelected] = useState<any>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(false);

  async function load() {
    setError(null); setLoading(true); setSelected(null);
    try {
      if (resource === 'services') setData(await api.infraListServices(context));
      else if (resource === 'pods') setData(await api.infraListPods(context, namespace));
      else if (resource === 'deployments') setData(await api.infraListDeployments(context, namespace));
      else if (resource === 'argocd') setData(await api.infraListArgocd(context));
    } catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }

  useEffect(() => { setContext(LIVE_DEFAULT_CONTEXT[resource]); }, [resource]);
  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [resource, context, namespace]);

  function safeDo(fn: () => Promise<any>) {
    return async () => {
      try { setSelected(await fn()); }
      catch (e: any) { setError(e); }
    };
  }

  const items: any[] = Array.isArray(data)
    ? data
    : Array.isArray(data?.services) ? data.services
    : Array.isArray(data?.pods) ? data.pods
    : Array.isArray(data?.deployments) ? data.deployments
    : Array.isArray(data?.apps) ? data.apps
    : [];

  function rowActions(row: any) {
    const name = row.name || row.Name || row.ID || row.id || '';
    if (resource === 'services') {
      return [
        { label: 'Inspect', onClick: safeDo(() => api.infraInspectService(name, context)) },
        { label: 'Logs (200 lines)', onClick: safeDo(() => api.infraServiceLogs(name, 200, context)) },
        { label: 'Restart', destructive: true, confirm: `Restart ${name}?`, onClick: safeDo(() => api.infraRestartService(name, context)) },
      ];
    }
    if (resource === 'pods') {
      return [
        { label: 'Logs (200 lines)', onClick: safeDo(() => api.infraPodLogs(row.namespace || namespace, name, 200, context)) },
      ];
    }
    if (resource === 'argocd') {
      return [
        { label: 'Sync', destructive: true, confirm: `Sync ${name}?`, onClick: safeDo(() => api.infraSyncArgocd(name, context)) },
      ];
    }
    return [];
  }

  return (
    <div>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="filter-bar" style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
        {(['services', 'pods', 'deployments', 'argocd'] as LiveResource[]).map(r => (
          <button key={r} className={`btn ${resource === r ? 'active' : ''}`} onClick={() => setResource(r)}>
            {r}
          </button>
        ))}
        <label style={{ display: 'flex', flexDirection: 'column', fontSize: 12 }}>
          <span>Context</span>
          <select value={context} onChange={e => setContext(e.target.value)}>
            <option value="swarm">swarm (swarm)</option>
            <option value="acme-prod">acme-prod (k8s)</option>
            <option value="acme-test">acme-test (k8s)</option>
          </select>
        </label>
        {(resource === 'pods' || resource === 'deployments') && (
          <label style={{ display: 'flex', flexDirection: 'column', fontSize: 12 }}>
            <span>Namespace</span>
            <input type="text" value={namespace} onChange={e => setNamespace(e.target.value)} />
          </label>
        )}
        <button className="btn" onClick={() => void load()}>&#8635; Refresh</button>
      </div>

      {loading && <p>Loading&hellip;</p>}

      {items.length === 0 && !loading && data && (
        <div style={{ marginTop: 12 }}>
          <p className="empty">No structured items returned. Raw response:</p>
          <JsonViewer data={data} />
        </div>
      )}

      {items.length > 0 && (
        <div className="table-scroll">
          <table className="data-table">
            <thead><tr><th>Name</th><th>Details</th><th style={{ width: 60 }}>Actions</th></tr></thead>
            <tbody>
              {items.map((row, i) => {
                const name = row.name || row.Name || row.ID || row.id || JSON.stringify(row).slice(0, 40);
                const detail = row.status || row.Status || row.image || row.Image || row.health || row.sync_status || '';
                return (
                  <tr key={i}>
                    <td><strong>{name}</strong></td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>{detail || '—'}</td>
                    <td><ActionMenu items={rowActions(row)} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <div style={{ marginTop: 16 }}>
          <h3>Response</h3>
          <JsonViewer data={selected} />
        </div>
      )}
    </div>
  );
}

export default function Infra() {
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingSecrets, setEditingSecrets] = useState({ hasSshKey: false, hasKubeconfig: false });
  const [form, setForm] = useState<InfraFormData>({ ...emptyForm });
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);

  const [provisioningId, setProvisioningId] = useState<string | null>(null);
  const [provisionLogs, setProvisionLogs] = useState<Record<string, any[]>>({});
  const [expandedLogId, setExpandedLogId] = useState<string | null>(null);

  const [showLiveInspector, setShowLiveInspector] = useState(false);

  const load = () => {
    setLoading(true);
    setError(null);
    api.listInfra()
      .then(r => { setRows(r || []); setLoading(false); })
      .catch((e: any) => { setError(e); setLoading(false); });
  };

  useEffect(load, []);

  const openCreate = () => {
    setEditingId(null);
    setEditingSecrets({ hasSshKey: false, hasKubeconfig: false });
    setForm({ ...emptyForm });
    setFormError('');
    setShowForm(true);
  };

  const openEdit = (row: any) => {
    setEditingId(row.id);
    setEditingSecrets({ hasSshKey: !!row.has_ssh_key, hasKubeconfig: !!row.has_kubeconfig });
    setForm({
      name: row.name || '',
      kind: row.kind || 'ssh_host',
      host: row.host || '',
      ssh_user: row.ssh_user || '',
      ssh_port: row.ssh_port != null ? String(row.ssh_port) : '22',
      ssh_key_ref: row.ssh_key_ref || '',
      ssh_private_key: '',
      kubeconfig: '',
      docker_context: row.docker_context || '',
      hosts_aegis: !!row.hosts_aegis,
      setup_command: row.setup_command || '',
      setup_files: Array.isArray(row.setup_files)
        ? row.setup_files.map((f: any) => ({ path: f.path || '', content: f.content || '', mode: f.mode || '' }))
        : [],
    });
    setFormError('');
    setShowForm(true);
  };

  const addSetupFile = () => {
    setForm(f => ({ ...f, setup_files: [...f.setup_files, { path: '', content: '', mode: '' }] }));
  };
  const removeSetupFile = (idx: number) => {
    setForm(f => ({ ...f, setup_files: f.setup_files.filter((_, i) => i !== idx) }));
  };
  const updateSetupFile = (idx: number, field: keyof SetupFile, value: string) => {
    setForm(f => ({
      ...f,
      setup_files: f.setup_files.map((sf, i) => (i === idx ? { ...sf, [field]: value } : sf)),
    }));
  };

  const handleSave = async () => {
    if (!form.name.trim()) { setFormError('Name is required'); return; }
    setSaving(true);
    setFormError('');
    try {
      const payload: Record<string, any> = {
        name: form.name.trim(),
        kind: form.kind,
        host: form.host.trim(),
        hosts_aegis: form.hosts_aegis,
      };
      if (form.ssh_user.trim()) payload.ssh_user = form.ssh_user.trim();
      if (form.ssh_port.trim()) {
        const port = Number(form.ssh_port);
        if (Number.isNaN(port)) { setFormError('SSH port must be a number'); setSaving(false); return; }
        payload.ssh_port = port;
      }
      if (form.ssh_key_ref.trim()) payload.ssh_key_ref = form.ssh_key_ref.trim();
      if (form.ssh_private_key.trim()) payload.ssh_private_key = form.ssh_private_key;
      if (form.kubeconfig.trim()) payload.kubeconfig = form.kubeconfig;
      if (form.docker_context.trim()) payload.docker_context = form.docker_context.trim();
      if (form.setup_command.trim()) payload.setup_command = form.setup_command.trim();
      payload.setup_files = form.setup_files
        .filter(f => f.path.trim())
        .map(f => ({
          path: f.path.trim(),
          content: f.content,
          ...(f.mode.trim() ? { mode: f.mode.trim() } : {}),
        }));

      if (editingId) {
        await api.updateInfra(editingId, payload);
      } else {
        await api.createInfra(payload);
      }
      setShowForm(false);
      setEditingId(null);
      load();
    } catch (err: any) {
      setFormError(err.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string, name: string) => {
    if (!confirm(`Delete infrastructure entry "${name}"?`)) return;
    try {
      await api.deleteInfra(id);
      load();
    } catch (err: any) {
      setError(err);
    }
  };

  const handleProvision = async (id: string) => {
    setProvisioningId(id);
    setError(null);
    try {
      const result = await api.provisionInfra(id);
      setProvisionLogs(prev => ({ ...prev, [id]: result?.log || [] }));
      setExpandedLogId(id);
    } catch (err: any) {
      setError(err);
    } finally {
      setProvisioningId(null);
      load();
    }
  };

  return (
    <div>
      <div className="page-header-row">
        <div>
          <h1 className="page-title">Infrastructure</h1>
          <p className="page-subtitle">Registry of hosts AEGIS can provision and operate. {rows.length} entries.</p>
        </div>
        <button className="btn btn-primary" onClick={openCreate}>+ Add infrastructure</button>
      </div>

      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {showForm && (
        <div className="modal-overlay" onClick={() => setShowForm(false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div className="modal-header">
              <h3>{editingId ? 'Edit infrastructure' : 'New infrastructure'}</h3>
              <button className="modal-close" onClick={() => setShowForm(false)}>&times;</button>
            </div>
            <div className="modal-body">
              {formError && <div className="msg-error" style={{ marginBottom: '0.75rem' }}>{formError}</div>}

              <div className="form-row">
                <div className="form-group">
                  <label>Name</label>
                  <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="e.g. baa (swarm leader)" />
                </div>
                <div className="form-group">
                  <label>Kind</label>
                  <select value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value })}>
                    {INFRA_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
                  </select>
                </div>
              </div>

              <div className="form-group">
                <label>Host</label>
                <input value={form.host} onChange={e => setForm({ ...form, host: e.target.value })} placeholder="hostname or IP" className="mono" />
              </div>

              <div className="form-row">
                <div className="form-group">
                  <label>SSH user</label>
                  <input value={form.ssh_user} onChange={e => setForm({ ...form, ssh_user: e.target.value })} placeholder="e.g. ubuntu" />
                </div>
                <div className="form-group">
                  <label>SSH port</label>
                  <input value={form.ssh_port} onChange={e => setForm({ ...form, ssh_port: e.target.value })} placeholder="22" />
                </div>
              </div>

              <div className="form-group">
                <label>SSH private key (stored encrypted)</label>
                <textarea
                  rows={4}
                  value={form.ssh_private_key}
                  onChange={e => setForm({ ...form, ssh_private_key: e.target.value })}
                  className="mono"
                  placeholder={editingSecrets.hasSshKey
                    ? 'set — paste to replace, leave blank to keep'
                    : '-----BEGIN OPENSSH PRIVATE KEY-----'}
                />
              </div>

              <div className="form-group">
                <label>SSH key ref (optional if key pasted above)</label>
                <input value={form.ssh_key_ref} onChange={e => setForm({ ...form, ssh_key_ref: e.target.value })} placeholder="path to private key on core host" className="mono" />
              </div>

              {form.kind === 'k8s' && (
                <div className="form-group">
                  <label>Kubeconfig (stored encrypted)</label>
                  <textarea
                    rows={4}
                    value={form.kubeconfig}
                    onChange={e => setForm({ ...form, kubeconfig: e.target.value })}
                    className="mono"
                    placeholder={editingSecrets.hasKubeconfig
                      ? 'set — paste to replace, leave blank to keep'
                      : 'apiVersion: v1\nkind: Config\n...'}
                  />
                </div>
              )}

              <div className="form-group">
                <label>Docker context (optional)</label>
                <input value={form.docker_context} onChange={e => setForm({ ...form, docker_context: e.target.value })} placeholder="e.g. swarm" className="mono" />
              </div>

              <div className="form-group">
                <label>
                  <input
                    type="checkbox"
                    checked={form.hosts_aegis}
                    onChange={e => setForm({ ...form, hosts_aegis: e.target.checked })}
                    style={{ width: 'auto', marginRight: '0.4rem' }}
                  />
                  This host runs AEGIS itself
                </label>
              </div>

              <div className="form-group">
                <label>Setup command (optional)</label>
                <textarea rows={3} value={form.setup_command} onChange={e => setForm({ ...form, setup_command: e.target.value })} className="mono" placeholder="e.g. bash /opt/aegis/setup.sh" />
              </div>

              <div className="form-group">
                <label>Setup files</label>
                {form.setup_files.length === 0 && (
                  <p className="meta" style={{ marginBottom: '0.5rem' }}>No setup files. Add one to write config/files to the host during provisioning.</p>
                )}
                {form.setup_files.map((sf, idx) => (
                  <div key={idx} className="card" style={{ marginBottom: '0.6rem', padding: '0.75rem' }}>
                    <div className="cfg-row">
                      <span className="cfg-label">Path</span>
                      <input value={sf.path} onChange={e => updateSetupFile(idx, 'path', e.target.value)} placeholder="/etc/aegis/config.yml" className="mono" />
                    </div>
                    <div className="cfg-row">
                      <span className="cfg-label">Mode (optional)</span>
                      <input value={sf.mode} onChange={e => updateSetupFile(idx, 'mode', e.target.value)} placeholder="0644" className="mono" style={{ maxWidth: 120 }} />
                    </div>
                    <div className="form-group" style={{ marginBottom: 0, marginTop: '0.4rem' }}>
                      <label>Content</label>
                      <textarea rows={3} value={sf.content} onChange={e => updateSetupFile(idx, 'content', e.target.value)} className="mono" />
                    </div>
                    <button className="btn btn-sm btn-icon-danger" style={{ marginTop: '0.4rem' }} onClick={() => removeSetupFile(idx)}>Remove file</button>
                  </div>
                ))}
                <button className="btn btn-sm" onClick={addSetupFile}>+ Add setup file</button>
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
        <div className="loading">Loading infrastructure...</div>
      ) : rows.length === 0 ? (
        <div className="empty">No infrastructure entries yet. Add one to let AEGIS provision and monitor a host.</div>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Kind</th>
                <th>Host</th>
                <th>Status</th>
                <th>Hosts AEGIS</th>
                <th>Last provisioned</th>
                <th style={{ width: 220 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(row => {
                const isProvisioning = provisioningId === row.id || row.status === 'provisioning';
                const log = provisionLogs[row.id];
                const isExpanded = expandedLogId === row.id;
                return (
                  <tr key={row.id}>
                    <td>
                      <strong>{row.name}</strong>
                      <div className="meta mono">{row.slug}</div>
                    </td>
                    <td>{row.kind}</td>
                    <td className="mono">
                      {row.host}{row.ssh_port ? `:${row.ssh_port}` : ''}
                      {row.has_ssh_key && <span title="SSH key stored" style={{ marginLeft: 4 }}>&#128273;</span>}
                    </td>
                    <td>
                      <span className={statusBadgeClass(row.status)}>{row.status}</span>
                      {row.status === 'error' && row.last_error && (
                        <div className="msg-error" style={{ marginTop: 4, maxWidth: 260, wordBreak: 'break-word' }}>{row.last_error}</div>
                      )}
                    </td>
                    <td>{row.hosts_aegis ? <span className="badge badge-success">yes</span> : <span className="badge badge-neutral">no</span>}</td>
                    <td className="meta">{row.last_provisioned_at ? new Date(row.last_provisioned_at).toLocaleString() : '—'}</td>
                    <td>
                      <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
                        <button className="btn btn-sm" disabled={isProvisioning} onClick={() => handleProvision(row.id)}>
                          {isProvisioning ? 'Provisioning...' : 'Provision'}
                        </button>
                        {log && (
                          <button className="btn btn-sm" onClick={() => setExpandedLogId(isExpanded ? null : row.id)}>
                            {isExpanded ? 'Hide log' : 'View log'}
                          </button>
                        )}
                        <button className="btn-icon" title="Edit" onClick={() => openEdit(row)}>&#9998;</button>
                        <button className="btn-icon btn-icon-danger" title="Delete" onClick={() => handleDelete(row.id, row.name)}>&times;</button>
                      </div>
                      {isExpanded && log && (
                        <div style={{ marginTop: 8 }}>
                          {log.length === 0 ? (
                            <p className="meta">No steps recorded.</p>
                          ) : (
                            <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: '0.8rem' }}>
                              {log.map((step: any, i: number) => (
                                <li key={i} style={{ padding: '0.35rem 0', borderBottom: '1px solid var(--border)' }}>
                                  <span className={`badge ${step.ok ? 'badge-success' : 'badge-error'}`} style={{ marginRight: 6 }}>
                                    {step.ok ? 'ok' : 'failed'}
                                  </span>
                                  <strong>{step.step}</strong>
                                  {step.exit_code != null && <span className="meta"> (exit {step.exit_code})</span>}
                                  {step.error && <div className="msg-error" style={{ marginTop: 2 }}>{step.error}</div>}
                                  {step.stderr && (
                                    <pre style={{ fontSize: '0.72rem', color: 'var(--text-muted)', marginTop: 4, whiteSpace: 'pre-wrap' }}>{step.stderr}</pre>
                                  )}
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <section style={{ marginTop: 32 }}>
        <div className="section-header-row">
          <h2 className="section-title">Live service/pod inspector</h2>
          <button className="btn btn-sm" onClick={() => setShowLiveInspector(v => !v)}>
            {showLiveInspector ? 'Hide' : 'Show'}
          </button>
        </div>
        {showLiveInspector && <LiveInspector />}
      </section>
    </div>
  );
}
