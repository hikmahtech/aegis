import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import ActionMenu from '../components/ActionMenu';
import JsonViewer from '../components/JsonViewer';

type Resource = 'services' | 'pods' | 'deployments' | 'argocd';

const DEFAULT_CONTEXT: Record<Resource, string> = {
  services: 'swarm',
  pods: 'acme-prod',
  deployments: 'acme-prod',
  argocd: 'acme-prod',
};

export default function Infra() {
  const [resource, setResource] = useState<Resource>('services');
  const [context, setContext] = useState<string>(DEFAULT_CONTEXT.services);
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

  useEffect(() => { setContext(DEFAULT_CONTEXT[resource]); }, [resource]);
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
      <h1 className="page-title">Infrastructure</h1>
      <p className="page-subtitle">Operate the stack.</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="filter-bar" style={{ alignItems: 'flex-end', flexWrap: 'wrap' }}>
        {(['services', 'pods', 'deployments', 'argocd'] as Resource[]).map(r => (
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
        <button className="btn" onClick={() => void load()}>↻ Refresh</button>
      </div>

      {loading && <p>Loading…</p>}

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
                    <td style={{ fontSize: 12, color: '#666' }}>{detail || '—'}</td>
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
