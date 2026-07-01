import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

type Agent = {
  id: string;
  name: string;
  role: string;
  capabilities?: string[];
  model_tier?: string;
  active?: boolean;
  telegram_topic_id?: number | null;
};

export default function Personalities() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    api
      .listAgents()
      .then(data => { setAgents(data || []); setLoading(false); })
      .catch(err => { setError(err); setLoading(false); });
  }, []);

  if (loading) return <div className="loading">Loading personalities…</div>;

  return (
    <div>
      <h1 className="page-title">Personalities</h1>
      <p className="page-subtitle">Who does what?</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="grid">
        {agents.map(a => {
          const caps = Array.isArray(a.capabilities) ? a.capabilities : [];
          return (
            <Link
              key={a.id}
              to={`/personalities/${a.id}`}
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
        {agents.length === 0 && <p className="empty">No personalities configured.</p>}
      </div>
    </div>
  );
}
