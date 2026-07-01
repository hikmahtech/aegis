import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';

const ACTIVE_STATUSES = new Set(['pending', 'accepted', 'running', 'processing', 'queued']);
const POLL_INTERVAL_MS = 5000;

export default function Content() {
  const [items, setItems] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const fetchOnce = () => {
      api.listKnowledgeContent(200)
        .then(data => {
          if (cancelled) return;
          const rows = data || [];
          setItems(rows);
          setLoading(false);
          const hasActive = rows.some(r => ACTIVE_STATUSES.has(String(r.status || '').toLowerCase()));
          if (hasActive) {
            timer = setTimeout(fetchOnce, POLL_INTERVAL_MS);
          }
        })
        .catch(err => {
          if (cancelled) return;
          setError(err.message || 'Failed to load content');
          setLoading(false);
        });
    };

    fetchOnce();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  if (loading) return <div className="loading">Loading ingested content...</div>;
  if (error) return <div className="error">{error}</div>;

  const activeCount = items.filter(r => ACTIVE_STATUSES.has(String(r.status || '').toLowerCase())).length;

  return (
    <div>
      <h1 className="page-title">Content</h1>
      <p className="page-subtitle">
        Documents ingested into the knowledge graph.
        {activeCount > 0 && <> &mdash; <strong>{activeCount} still processing</strong> (auto-refreshing)</>}
      </p>

      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Title</th>
              <th>Source Type</th>
              <th>Chunks</th>
              <th>Triples</th>
              <th>Status</th>
              <th>Ingested</th>
            </tr>
          </thead>
          <tbody>
            {items.map(c => {
              const id = c.content_id || c.id;
              return (
                <tr key={id}>
                  <td>
                    {id ? (
                      <Link to={`/content/${encodeURIComponent(id)}`}>
                        <strong>{c.title || id}</strong>
                      </Link>
                    ) : (
                      <strong>{c.title || '\u2014'}</strong>
                    )}
                  </td>
                  <td>{c.source_type || '\u2014'}</td>
                  <td className="mono">{c.chunks_total ?? c.chunks ?? '\u2014'}</td>
                  <td className="mono">{c.triples_created ?? c.triples ?? '\u2014'}</td>
                  <td>{c.status || '\u2014'}</td>
                  <td>{c.created_at ? new Date(c.created_at).toLocaleString() : '\u2014'}</td>
                </tr>
              );
            })}
            {items.length === 0 && (
              <tr>
                <td colSpan={6} className="empty">No content ingested yet</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
