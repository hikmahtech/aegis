import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import ActionMenu from '../components/ActionMenu';
import JsonViewer from '../components/JsonViewer';

const INFRA_KINDS = ['ssh_host', 'swarm', 'docker', 'k8s', 'cloud'];

interface SetupFile {
  path: string;
  content: string;
  mode: string;
}

// Coding agent (remote script) block — non-secret; the SSH identity (host,
// user, port, encrypted key) comes from the entry's own fields.
interface CodingAccount {
  label: string;
  config_dir: string;
}

interface CodingRoute {
  org: string;
  engine: string;
  account: string;
}

interface CodingFormData {
  enabled: boolean;
  repo_base: string;
  claude_binary: string;
  kimi_binary: string;
  accounts: CodingAccount[];
  default_account: string;
  routes: CodingRoute[];
  default_engine: string;
  tmux_session: string;
  tmux_window_cap: string;
  kimi_host_slug: string;
  self_repo_path: string;
  runbooks_dir: string;
}

const emptyCoding: CodingFormData = {
  enabled: false,
  repo_base: '',
  claude_binary: '',
  kimi_binary: '',
  accounts: [],
  default_account: '',
  routes: [],
  default_engine: 'kimi',
  tmux_session: 'remote',
  tmux_window_cap: '10',
  kimi_host_slug: '',
  self_repo_path: '',
  runbooks_dir: '',
};

function codingFromRow(coding: any): CodingFormData {
  if (!coding || typeof coding !== 'object' || Object.keys(coding).length === 0) {
    return { ...emptyCoding };
  }
  const claude = coding.engines?.claude || {};
  const kimi = coding.engines?.kimi || {};
  const routing = coding.routing || {};
  const tmux = coding.tmux || {};
  return {
    enabled: !!coding.enabled,
    repo_base: coding.repo_base || '',
    claude_binary: claude.binary_path || '',
    kimi_binary: kimi.binary_path || '',
    accounts: Object.entries(claude.config_dirs || {}).map(([label, dir]) => ({
      label,
      config_dir: String(dir),
    })),
    default_account: claude.default_account || '',
    routes: Object.entries(routing.orgs || {}).map(([org, r]: [string, any]) => ({
      org,
      engine: r?.engine || 'claude',
      account: r?.account || '',
    })),
    default_engine: routing.default_engine || 'kimi',
    tmux_session: tmux.session || 'remote',
    tmux_window_cap: tmux.window_cap != null ? String(tmux.window_cap) : '10',
    kimi_host_slug: coding.kimi_host_slug || '',
    self_repo_path: coding.self_repo_path || '',
    runbooks_dir: coding.runbooks_dir || '',
  };
}

function codingToPayload(c: CodingFormData): Record<string, any> {
  const config_dirs: Record<string, string> = {};
  for (const a of c.accounts) {
    if (a.label.trim()) config_dirs[a.label.trim()] = a.config_dir.trim();
  }
  const orgs: Record<string, any> = {};
  for (const r of c.routes) {
    if (r.org.trim()) orgs[r.org.trim()] = { engine: r.engine, account: r.account.trim() };
  }
  return {
    enabled: c.enabled,
    repo_base: c.repo_base.trim(),
    engines: {
      claude: {
        binary_path: c.claude_binary.trim(),
        config_dirs,
        default_account: c.default_account.trim(),
      },
      kimi: { binary_path: c.kimi_binary.trim() },
    },
    routing: { orgs, default_engine: c.default_engine },
    tmux: { session: c.tmux_session.trim() || 'remote', window_cap: Number(c.tmux_window_cap) || 10 },
    kimi_host_slug: c.kimi_host_slug.trim() || null,
    self_repo_path: c.self_repo_path.trim(),
    runbooks_dir: c.runbooks_dir.trim(),
  };
}

function codingTouched(c: CodingFormData): boolean {
  return (
    c.enabled ||
    !!(c.repo_base.trim() || c.claude_binary.trim() || c.kimi_binary.trim() ||
       c.kimi_host_slug.trim() || c.self_repo_path.trim() || c.runbooks_dir.trim() ||
       c.default_account.trim()) ||
    c.accounts.length > 0 ||
    c.routes.length > 0 ||
    c.default_engine !== 'kimi' ||
    c.tmux_session !== 'remote' ||
    c.tmux_window_cap !== '10'
  );
}

interface InfraFormData {
  name: string;
  kind: string;
  host: string;
  ssh_user: string;
  ssh_port: string;
  ssh_key_ref: string;
  // Write-only: sent only when non-empty; server never returns the values,
  // only has_* booleans.
  ssh_private_key: string;
  kubeconfig: string;
  auth_env: string; // KEY=value lines, parsed to a dict on save
  aws_credentials_file: string;
  gcp_service_account_json: string;
  docker_context: string;
  hosts_aegis: boolean;
  read_only: boolean;
  setup_command: string;
  setup_files: SetupFile[];
  coding: CodingFormData;
  // Cloud account block (kind=cloud) — non-secret; the credentials reuse the
  // aws_credentials_file / gcp_service_account_json write-only fields above.
  cloud_provider: string;
  cloud_default_profile: string;
  cloud_region: string;
  cloud_project: string;
  // Cloud account reference (kind=k8s).
  cloud_slug: string;
  cloud_profile: string;
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
  auth_env: '',
  aws_credentials_file: '',
  gcp_service_account_json: '',
  docker_context: '',
  hosts_aegis: false,
  read_only: false,
  setup_command: '',
  setup_files: [],
  coding: { ...emptyCoding },
  cloud_provider: 'aws',
  cloud_default_profile: '',
  cloud_region: '',
  cloud_project: '',
  cloud_slug: '',
  cloud_profile: '',
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

// ── Per-entry k8s cluster panel (kind=k8s rows with a stored kubeconfig) ───
function K8sClusterPanel({ infraId, readOnly }: { infraId: string; readOnly: boolean }) {
  const [namespace, setNamespace] = useState('default');
  const [pods, setPods] = useState<any[]>([]);
  const [deployments, setDeployments] = useState<any[]>([]);
  const [logs, setLogs] = useState<{ pod: string; text: string } | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

  const load = async () => {
    setLoading(true); setError(''); setMsg(''); setLogs(null);
    try {
      const [p, d] = await Promise.all([
        api.infraK8sPods(infraId, namespace),
        api.infraK8sDeployments(infraId, namespace),
      ]);
      setPods(p?.pods || []);
      setDeployments(d?.deployments || []);
    } catch (e: any) { setError(e.message || 'load failed'); }
    finally { setLoading(false); }
  };

  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [infraId]);

  const showLogs = async (pod: string) => {
    setError('');
    try {
      const r = await api.infraK8sPodLogs(infraId, namespace, pod, 200);
      setLogs({ pod, text: r?.logs || '(no output)' });
    } catch (e: any) { setError(e.message || 'logs failed'); }
  };

  const restart = async (name: string) => {
    if (!confirm(`Restart deployment ${name} in ${namespace}?`)) return;
    setError(''); setMsg('');
    try {
      const r = await api.infraK8sRestartDeployment(infraId, namespace, name);
      setMsg(r?.output || 'restart submitted');
    } catch (e: any) { setError(e.message || 'restart failed'); }
  };

  return (
    <div className="card" style={{ marginTop: 8, padding: '0.75rem' }}>
      <div className="filter-bar" style={{ alignItems: 'flex-end' }}>
        <label style={{ display: 'flex', flexDirection: 'column', fontSize: 12 }}>
          <span>Namespace</span>
          <input value={namespace} onChange={e => setNamespace(e.target.value)} className="mono" />
        </label>
        <button className="btn btn-sm" disabled={loading} onClick={() => void load()}>
          {loading ? 'Loading…' : '⟳ Refresh'}
        </button>
      </div>
      {error && <div className="msg-error" style={{ marginTop: 6 }}>{error}</div>}
      {msg && <p className="msg-success" style={{ marginTop: 6 }}>{msg}</p>}

      <h4 style={{ margin: '0.6rem 0 0.3rem' }}>Deployments</h4>
      {deployments.length === 0 ? <p className="meta">None in this namespace.</p> : (
        <table className="data-table">
          <thead><tr><th>Name</th><th>Ready</th><th>Images</th><th /></tr></thead>
          <tbody>
            {deployments.map(d => (
              <tr key={d.name}>
                <td className="mono">{d.name}</td>
                <td>{d.ready}</td>
                <td className="mono" style={{ fontSize: 12 }}>{(d.images || []).join(', ')}</td>
                <td>{!readOnly && <button className="btn btn-sm" onClick={() => void restart(d.name)}>Restart</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4 style={{ margin: '0.6rem 0 0.3rem' }}>Pods</h4>
      {pods.length === 0 ? <p className="meta">None in this namespace.</p> : (
        <table className="data-table">
          <thead><tr><th>Name</th><th>Phase</th><th>Ready</th><th>Restarts</th><th>Node</th><th /></tr></thead>
          <tbody>
            {pods.map(p => (
              <tr key={p.name}>
                <td className="mono">{p.name}</td>
                <td>{p.phase}</td>
                <td>{p.ready}</td>
                <td>{p.restarts}</td>
                <td className="mono" style={{ fontSize: 12 }}>{p.node}</td>
                <td><button className="btn btn-sm" onClick={() => void showLogs(p.name)}>Logs</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {logs && (
        <div style={{ marginTop: 8 }}>
          <h4 style={{ margin: '0 0 0.3rem' }}>Logs — {logs.pod}</h4>
          <pre style={{ fontSize: '0.72rem', maxHeight: 300, overflow: 'auto', whiteSpace: 'pre-wrap' }}>{logs.text}</pre>
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
  const [editingSecrets, setEditingSecrets] = useState({
    hasSshKey: false, hasKubeconfig: false, hasAuthEnv: false, hasAwsCredentials: false, hasGcpServiceAccount: false,
  });
  const [form, setForm] = useState<InfraFormData>({ ...emptyForm });
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);

  const [showCoding, setShowCoding] = useState(false);
  const [editingHadCoding, setEditingHadCoding] = useState(false);
  const [provisioningId, setProvisioningId] = useState<string | null>(null);
  const [provisionLogs, setProvisionLogs] = useState<Record<string, any[]>>({});
  const [expandedLogId, setExpandedLogId] = useState<string | null>(null);
  const [expandedK8sId, setExpandedK8sId] = useState<string | null>(null);

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
    setEditingSecrets({ hasSshKey: false, hasKubeconfig: false, hasAuthEnv: false, hasAwsCredentials: false, hasGcpServiceAccount: false });
    setForm({ ...emptyForm, coding: { ...emptyCoding } });
    setShowCoding(false);
    setEditingHadCoding(false);
    setFormError('');
    setShowForm(true);
  };

  const openEdit = (row: any) => {
    setEditingId(row.id);
    setEditingSecrets({
      hasSshKey: !!row.has_ssh_key,
      hasKubeconfig: !!row.has_kubeconfig,
      hasAuthEnv: !!row.has_auth_env,
      hasAwsCredentials: !!row.has_aws_credentials,
      hasGcpServiceAccount: !!row.has_gcp_service_account,
    });
    setForm({
      name: row.name || '',
      kind: row.kind || 'ssh_host',
      host: row.host || '',
      ssh_user: row.ssh_user || '',
      ssh_port: row.ssh_port != null ? String(row.ssh_port) : '22',
      ssh_key_ref: row.ssh_key_ref || '',
      ssh_private_key: '',
      kubeconfig: '',
      auth_env: '',
      aws_credentials_file: '',
      gcp_service_account_json: '',
      docker_context: row.docker_context || '',
      hosts_aegis: !!row.hosts_aegis,
      read_only: !!row.read_only,
      setup_command: row.setup_command || '',
      setup_files: Array.isArray(row.setup_files)
        ? row.setup_files.map((f: any) => ({ path: f.path || '', content: f.content || '', mode: f.mode || '' }))
        : [],
      coding: codingFromRow(row.coding),
      cloud_provider: row.cloud?.provider || 'aws',
      cloud_default_profile: row.cloud?.default_profile || '',
      cloud_region: row.cloud?.region || '',
      cloud_project: row.cloud?.project || '',
      cloud_slug: row.cloud?.cloud_slug || '',
      cloud_profile: row.cloud?.profile || '',
    });
    setShowCoding(!!row.coding?.enabled);
    setEditingHadCoding(!!row.coding && Object.keys(row.coding).length > 0);
    setFormError('');
    setShowForm(true);
  };

  const setCoding = (patch: Partial<CodingFormData>) => {
    setForm(f => ({ ...f, coding: { ...f.coding, ...patch } }));
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
        read_only: form.read_only,
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
      if (form.auth_env.trim()) {
        const env: Record<string, string> = {};
        for (const line of form.auth_env.split('\n')) {
          const t = line.trim();
          if (!t || t.startsWith('#')) continue;
          const eq = t.indexOf('=');
          if (eq <= 0) { setFormError(`Auth env: expected KEY=value, got "${t}"`); setSaving(false); return; }
          env[t.slice(0, eq).trim()] = t.slice(eq + 1).trim();
        }
        if (Object.keys(env).length) payload.auth_env = env;
      }
      if (form.aws_credentials_file.trim()) payload.aws_credentials_file = form.aws_credentials_file;
      if (form.gcp_service_account_json.trim()) payload.gcp_service_account_json = form.gcp_service_account_json;
      if (form.docker_context.trim()) payload.docker_context = form.docker_context.trim();
      if (form.setup_command.trim()) payload.setup_command = form.setup_command.trim();
      if (form.kind === 'cloud') {
        payload.cloud = form.cloud_provider === 'aws'
          ? { provider: 'aws', default_profile: form.cloud_default_profile.trim(), region: form.cloud_region.trim() }
          : { provider: 'gcp', project: form.cloud_project.trim() };
      } else if (form.kind === 'k8s') {
        payload.cloud = form.cloud_slug
          ? { cloud_slug: form.cloud_slug, profile: form.cloud_profile.trim() }
          : {};
      }
      if (form.kind !== 'k8s' && form.kind !== 'cloud' && (codingTouched(form.coding) || editingHadCoding)) {
        if (form.coding.tmux_window_cap.trim() && Number.isNaN(Number(form.coding.tmux_window_cap))) {
          setFormError('tmux window cap must be a number'); setSaving(false); return;
        }
        payload.coding = codingToPayload(form.coding);
      }
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
                  <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} placeholder="e.g. mgr-1 (swarm leader)" />
                </div>
                <div className="form-group">
                  <label>Kind</label>
                  <select value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value })}>
                    {INFRA_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
                  </select>
                </div>
              </div>

              {form.kind !== 'cloud' && (
                <>
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
                </>
              )}

              {form.kind === 'cloud' && (
                <>
                  <div className="form-group">
                    <label>Provider</label>
                    <select value={form.cloud_provider} onChange={e => setForm({ ...form, cloud_provider: e.target.value })}>
                      <option value="aws">aws</option>
                      <option value="gcp">gcp</option>
                    </select>
                  </div>

                  {form.cloud_provider === 'aws' ? (
                    <>
                      <div className="form-group">
                        <label>AWS credentials file (stored encrypted — multi-profile ini)</label>
                        <textarea
                          rows={5}
                          value={form.aws_credentials_file}
                          onChange={e => setForm({ ...form, aws_credentials_file: e.target.value })}
                          className="mono"
                          placeholder={editingSecrets.hasAwsCredentials
                            ? 'set — paste to replace, leave blank to keep'
                            : '[default]\naws_access_key_id = ...\naws_secret_access_key = ...\n[prod]\nrole_arn = ...\nsource_profile = default'}
                        />
                      </div>
                      <div className="form-group">
                        <label>Auth env (KEY=value per line, stored encrypted — alternative to the file)</label>
                        <textarea
                          rows={2}
                          value={form.auth_env}
                          onChange={e => setForm({ ...form, auth_env: e.target.value })}
                          className="mono"
                          placeholder={editingSecrets.hasAuthEnv
                            ? 'set — paste to replace, leave blank to keep'
                            : 'AWS_ACCESS_KEY_ID=...\nAWS_SECRET_ACCESS_KEY=...'}
                        />
                      </div>
                      <div className="form-row">
                        <div className="form-group">
                          <label>Default profile (AWS_PROFILE when none given)</label>
                          <input value={form.cloud_default_profile} onChange={e => setForm({ ...form, cloud_default_profile: e.target.value })} placeholder="e.g. prod" className="mono" />
                        </div>
                        <div className="form-group">
                          <label>Region</label>
                          <input value={form.cloud_region} onChange={e => setForm({ ...form, cloud_region: e.target.value })} placeholder="e.g. eu-west-2" className="mono" />
                        </div>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="form-group">
                        <label>GCP service account JSON (stored encrypted)</label>
                        <textarea
                          rows={5}
                          value={form.gcp_service_account_json}
                          onChange={e => setForm({ ...form, gcp_service_account_json: e.target.value })}
                          className="mono"
                          placeholder={editingSecrets.hasGcpServiceAccount
                            ? 'set — paste to replace, leave blank to keep'
                            : '{\n  "type": "service_account",\n  "project_id": "...",\n  ...\n}'}
                        />
                      </div>
                      <div className="form-group">
                        <label>Project</label>
                        <input value={form.cloud_project} onChange={e => setForm({ ...form, cloud_project: e.target.value })} placeholder="gcp project id" className="mono" />
                      </div>
                    </>
                  )}
                </>
              )}

              {form.kind === 'k8s' && (
                <>
                  <div className="form-row">
                    <div className="form-group">
                      <label>Cloud account (optional — pulls exec-plugin credentials)</label>
                      <select value={form.cloud_slug} onChange={e => setForm({ ...form, cloud_slug: e.target.value })}>
                        <option value="">(none — inline credentials below)</option>
                        {rows.filter(r => r.kind === 'cloud').map(r => (
                          <option key={r.slug} value={r.slug}>{r.slug} ({r.cloud?.provider})</option>
                        ))}
                      </select>
                    </div>
                    <div className="form-group">
                      <label>AWS profile override (else account default)</label>
                      <input value={form.cloud_profile} onChange={e => setForm({ ...form, cloud_profile: e.target.value })} placeholder="e.g. prod" className="mono" disabled={!form.cloud_slug} />
                    </div>
                  </div>

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

                  <div className="form-group">
                    <label>Auth env (KEY=value per line, stored encrypted — for exec-plugin kubeconfigs)</label>
                    <textarea
                      rows={3}
                      value={form.auth_env}
                      onChange={e => setForm({ ...form, auth_env: e.target.value })}
                      className="mono"
                      placeholder={editingSecrets.hasAuthEnv
                        ? 'set — paste to replace, leave blank to keep'
                        : 'AWS_ACCESS_KEY_ID=...\nAWS_SECRET_ACCESS_KEY=...\n# or AWS_PROFILE=myprofile with a credentials file below'}
                    />
                  </div>

                  <div className="form-group">
                    <label>AWS credentials file (optional, stored encrypted — for AWS_PROFILE users)</label>
                    <textarea
                      rows={3}
                      value={form.aws_credentials_file}
                      onChange={e => setForm({ ...form, aws_credentials_file: e.target.value })}
                      className="mono"
                      placeholder={editingSecrets.hasAwsCredentials
                        ? 'set — paste to replace, leave blank to keep'
                        : '[myprofile]\naws_access_key_id = ...\naws_secret_access_key = ...'}
                    />
                  </div>

                  <div className="form-group">
                    <label>GCP service account JSON (optional, stored encrypted — for GKE gke-gcloud-auth-plugin)</label>
                    <textarea
                      rows={3}
                      value={form.gcp_service_account_json}
                      onChange={e => setForm({ ...form, gcp_service_account_json: e.target.value })}
                      className="mono"
                      placeholder={editingSecrets.hasGcpServiceAccount
                        ? 'set — paste to replace, leave blank to keep'
                        : '{\n  "type": "service_account",\n  "project_id": "...",\n  ...\n}'}
                    />
                  </div>
                </>
              )}

              {form.kind !== 'cloud' && (
                <>
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
                    <label>
                      <input
                        type="checkbox"
                        checked={form.read_only}
                        onChange={e => setForm({ ...form, read_only: e.target.checked })}
                        style={{ width: 'auto', marginRight: '0.4rem' }}
                      />
                      Read-only — block mutating operations (k8s restarts, SSH provisioning)
                    </label>
                  </div>
                </>
              )}

              {form.kind !== 'k8s' && form.kind !== 'cloud' && (
                <div className="card" style={{ marginBottom: '0.75rem', padding: '0.75rem' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <strong>Coding agent (remote script)</strong>
                    <button className="btn btn-sm" onClick={() => setShowCoding(v => !v)}>
                      {showCoding ? 'Hide' : 'Configure'}
                    </button>
                  </div>
                  <p className="meta" style={{ margin: '0.3rem 0 0' }}>
                    Marks this entry as the host coding-CLI runs (kimi/claude) execute on.
                    Uses this entry&apos;s SSH host/user/port and stored key — at most one entry can be enabled.
                  </p>
                  {showCoding && (
                    <div style={{ marginTop: '0.6rem' }}>
                      <div className="form-group">
                        <label>
                          <input
                            type="checkbox"
                            checked={form.coding.enabled}
                            onChange={e => setCoding({ enabled: e.target.checked })}
                            style={{ width: 'auto', marginRight: '0.4rem' }}
                          />
                          Enabled — this entry is the remote-script / coding host
                        </label>
                      </div>

                      <div className="form-group">
                        <label>Repo base (workspace root on the host)</label>
                        <input value={form.coding.repo_base} onChange={e => setCoding({ repo_base: e.target.value })} placeholder="/home/deploy/Workspace" className="mono" />
                      </div>

                      <div className="form-row">
                        <div className="form-group">
                          <label>Claude binary path</label>
                          <input value={form.coding.claude_binary} onChange={e => setCoding({ claude_binary: e.target.value })} placeholder="/usr/local/bin/claude" className="mono" />
                        </div>
                        <div className="form-group">
                          <label>Kimi binary path</label>
                          <input value={form.coding.kimi_binary} onChange={e => setCoding({ kimi_binary: e.target.value })} placeholder="/usr/local/bin/kimi" className="mono" />
                        </div>
                      </div>

                      <div className="form-group">
                        <label>Claude accounts (label &rarr; CLAUDE_CONFIG_DIR on the host)</label>
                        {form.coding.accounts.map((a, idx) => (
                          <div key={idx} style={{ display: 'flex', gap: '0.4rem', marginBottom: '0.35rem' }}>
                            <input value={a.label} onChange={e => setCoding({ accounts: form.coding.accounts.map((x, i) => i === idx ? { ...x, label: e.target.value } : x) })} placeholder="label (e.g. personal)" style={{ maxWidth: 160 }} />
                            <input value={a.config_dir} onChange={e => setCoding({ accounts: form.coding.accounts.map((x, i) => i === idx ? { ...x, config_dir: e.target.value } : x) })} placeholder="/home/deploy/.claude-personal" className="mono" style={{ flex: 1 }} />
                            <button className="btn btn-sm btn-icon-danger" onClick={() => setCoding({ accounts: form.coding.accounts.filter((_, i) => i !== idx) })}>&times;</button>
                          </div>
                        ))}
                        <button className="btn btn-sm" onClick={() => setCoding({ accounts: [...form.coding.accounts, { label: '', config_dir: '' }] })}>+ Add account</button>
                      </div>

                      <div className="form-group">
                        <label>Default Claude account (fallback runs; empty = host default ~/.claude)</label>
                        <select value={form.coding.default_account} onChange={e => setCoding({ default_account: e.target.value })}>
                          <option value="">(host default)</option>
                          {form.coding.accounts.filter(a => a.label.trim()).map(a => (
                            <option key={a.label} value={a.label.trim()}>{a.label.trim()}</option>
                          ))}
                        </select>
                      </div>

                      <div className="form-group">
                        <label>Org routing (GitHub org &rarr; engine + account)</label>
                        {form.coding.routes.map((r, idx) => (
                          <div key={idx} style={{ display: 'flex', gap: '0.4rem', marginBottom: '0.35rem' }}>
                            <input value={r.org} onChange={e => setCoding({ routes: form.coding.routes.map((x, i) => i === idx ? { ...x, org: e.target.value } : x) })} placeholder="github-org" style={{ maxWidth: 160 }} />
                            <select value={r.engine} onChange={e => setCoding({ routes: form.coding.routes.map((x, i) => i === idx ? { ...x, engine: e.target.value } : x) })}>
                              <option value="claude">claude</option>
                              <option value="kimi">kimi</option>
                            </select>
                            <select value={r.account} onChange={e => setCoding({ routes: form.coding.routes.map((x, i) => i === idx ? { ...x, account: e.target.value } : x) })} disabled={r.engine !== 'claude'}>
                              <option value="">(default account)</option>
                              {form.coding.accounts.filter(a => a.label.trim()).map(a => (
                                <option key={a.label} value={a.label.trim()}>{a.label.trim()}</option>
                              ))}
                            </select>
                            <button className="btn btn-sm btn-icon-danger" onClick={() => setCoding({ routes: form.coding.routes.filter((_, i) => i !== idx) })}>&times;</button>
                          </div>
                        ))}
                        <button className="btn btn-sm" onClick={() => setCoding({ routes: [...form.coding.routes, { org: '', engine: 'claude', account: '' }] })}>+ Add routing rule</button>
                      </div>

                      <div className="form-row">
                        <div className="form-group">
                          <label>Default engine (unrouted orgs)</label>
                          <select value={form.coding.default_engine} onChange={e => setCoding({ default_engine: e.target.value })}>
                            <option value="kimi">kimi</option>
                            <option value="claude">claude</option>
                          </select>
                        </div>
                        <div className="form-group">
                          <label>Kimi host (infra slug, optional)</label>
                          <input value={form.coding.kimi_host_slug} onChange={e => setCoding({ kimi_host_slug: e.target.value })} placeholder="slug of another entry" className="mono" />
                        </div>
                      </div>

                      <div className="form-row">
                        <div className="form-group">
                          <label>tmux session</label>
                          <input value={form.coding.tmux_session} onChange={e => setCoding({ tmux_session: e.target.value })} placeholder="remote" className="mono" />
                        </div>
                        <div className="form-group">
                          <label>tmux window cap</label>
                          <input value={form.coding.tmux_window_cap} onChange={e => setCoding({ tmux_window_cap: e.target.value })} placeholder="10" />
                        </div>
                      </div>

                      <div className="form-row">
                        <div className="form-group">
                          <label>AEGIS self-repo path (under repo base)</label>
                          <input value={form.coding.self_repo_path} onChange={e => setCoding({ self_repo_path: e.target.value })} placeholder="personal/aegis" className="mono" />
                        </div>
                        <div className="form-group">
                          <label>Runbooks dir (worker-local)</label>
                          <input value={form.coding.runbooks_dir} onChange={e => setCoding({ runbooks_dir: e.target.value })} placeholder="/app/runbooks" className="mono" />
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {form.kind !== 'cloud' && (
              <>
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
              </>
              )}
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
                      {row.kind === 'cloud' ? (
                        <>
                          {row.cloud?.provider}
                          {(row.has_aws_credentials || row.has_gcp_service_account || row.has_auth_env) && <span title="Credentials stored" style={{ marginLeft: 4 }}>&#128273;</span>}
                          <div className="meta">
                            {row.cloud?.identity?.account_id || row.cloud?.identity?.project || row.cloud?.project || (row.cloud?.provider === 'aws' ? 'not yet provisioned' : '')}
                            {row.cloud?.default_profile ? ` · ${row.cloud.default_profile}` : ''}
                          </div>
                        </>
                      ) : (
                        <>{row.host}{row.ssh_port ? `:${row.ssh_port}` : ''}</>
                      )}
                      {row.kind === 'k8s' && row.cloud?.cloud_slug && <span className="badge badge-neutral" title="Credentials from cloud account" style={{ marginLeft: 4 }}>{row.cloud.cloud_slug}</span>}
                      {row.has_ssh_key && <span title="SSH key stored" style={{ marginLeft: 4 }}>&#128273;</span>}
                      {row.read_only && <span className="badge badge-neutral" title="Mutating operations blocked" style={{ marginLeft: 4 }}>read-only</span>}
                      {row.coding?.enabled && <span className="badge badge-success" title="Remote-script / coding-agent host" style={{ marginLeft: 4 }}>coding host</span>}
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
                        {row.kind === 'k8s' && row.has_kubeconfig && (
                          <button className="btn btn-sm" onClick={() => setExpandedK8sId(expandedK8sId === row.id ? null : row.id)}>
                            {expandedK8sId === row.id ? 'Hide cluster' : 'Cluster'}
                          </button>
                        )}
                        <button className="btn-icon" title="Edit" onClick={() => openEdit(row)}>&#9998;</button>
                        <button className="btn-icon btn-icon-danger" title="Delete" onClick={() => handleDelete(row.id, row.name)}>&times;</button>
                      </div>
                      {expandedK8sId === row.id && <K8sClusterPanel infraId={row.id} readOnly={!!row.read_only} />}
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
