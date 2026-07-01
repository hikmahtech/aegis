import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';

type Tab = 'library' | 'to-read';
type Filters = { source_tag: string; q: string };

const SOURCE_TAGS = ['', '#research', '#email', '#manual', '#chat'];

export default function References() {
  const [tab, setTab] = useState<Tab>('library');
  const [library, setLibrary] = useState<any[]>([]);
  const [failures, setFailures] = useState<any[]>([]);
  const [filters, setFilters] = useState<Filters>({ source_tag: '', q: '' });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>('');

  function loadLibrary() {
    setLoading(true);
    setError('');
    api
      .listReferences({
        limit: 200,
        source_tag: filters.source_tag || undefined,
        q: filters.q || undefined,
      })
      .then(rows => setLibrary(rows || []))
      .catch(err => setError(err.message || 'Failed to load references'))
      .finally(() => setLoading(false));
  }

  function loadFailures() {
    setLoading(true);
    setError('');
    api
      .listReferenceFailures(200)
      .then(rows => setFailures(rows || []))
      .catch(err => setError(err.message || 'Failed to load reading list'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (tab === 'library') loadLibrary();
    else loadFailures();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  function onSearch(e: React.FormEvent) {
    e.preventDefault();
    loadLibrary();
  }

  function switchTab(next: Tab) {
    if (next !== tab) setTab(next);
  }

  return (
    <div>
      <h1 className="page-title">References</h1>
      <p className="page-subtitle">
        Raphael's reading corpus — filed into the knowledge service. The "To Read" tab lists
        tasks that couldn't be filed automatically and need a human pass.
      </p>

      <div className="filter-bar" style={{ marginBottom: 12 }}>
        <button
          onClick={() => switchTab('library')}
          className={`btn ${tab === 'library' ? 'active' : ''}`}
        >
          Library ({library.length})
        </button>
        <button
          onClick={() => switchTab('to-read')}
          className={`btn ${tab === 'to-read' ? 'active' : ''}`}
        >
          To Read ({failures.length})
        </button>
      </div>

      {error && <div className="error">{error}</div>}

      {tab === 'library' && (
        <>
          <form onSubmit={onSearch} className="filter-bar" style={{ marginBottom: 12 }}>
            <input
              type="text"
              placeholder="Search references (semantic)…"
              value={filters.q}
              onChange={e => setFilters(f => ({ ...f, q: e.target.value }))}
              style={{ flex: 1, minWidth: 240 }}
            />
            <select
              value={filters.source_tag}
              onChange={e => setFilters(f => ({ ...f, source_tag: e.target.value }))}
            >
              {SOURCE_TAGS.map(t => (
                <option key={t || 'all'} value={t}>
                  {t || 'all sources'}
                </option>
              ))}
            </select>
            <button type="submit" className="btn">
              Search
            </button>
            <button
              type="button"
              className="btn"
              onClick={() => {
                setFilters({ source_tag: '', q: '' });
                setTimeout(loadLibrary, 0);
              }}
            >
              Reset
            </button>
          </form>

          {loading ? (
            <div className="loading">Loading references…</div>
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Source</th>
                    <th>Tags</th>
                    <th>Filed</th>
                  </tr>
                </thead>
                <tbody>
                  {library.map(r => {
                    const id = r.content_id || r.id;
                    const meta = r.metadata || {};
                    const tags: string[] = (r.tags || []).filter((t: string) => t !== 'gtd:reference');
                    // Legacy KS rows don't surface metadata.source_tag — fall
                    // back to the first '#'-prefixed entry in tags.
                    const tagSource = tags.find((t: string) => t.startsWith('#'));
                    const src = meta.source_tag || r.source_tag || tagSource || '—';
                    return (
                      <tr key={id || r.url}>
                        <td>
                          {id ? (
                            <Link to={`/content/${encodeURIComponent(id)}`}>
                              <strong>{r.title || r.url || id}</strong>
                            </Link>
                          ) : (
                            <strong>{r.title || r.url || '—'}</strong>
                          )}
                          {r.url && r.url.startsWith('http') && (
                            <div style={{ fontSize: '0.85em', opacity: 0.7, wordBreak: 'break-word' }}>
                              <a href={r.url} target="_blank" rel="noopener noreferrer">
                                {r.url}
                              </a>
                            </div>
                          )}
                        </td>
                        <td className="mono">{src}</td>
                        <td>
                          {tags.length > 0 ? (
                            tags.map((t: string) => (
                              <span key={t} className="chip" style={{ marginRight: 4 }}>
                                {t}
                              </span>
                            ))
                          ) : (
                            <span style={{ opacity: 0.5 }}>—</span>
                          )}
                        </td>
                        <td>
                          {r.created_at || r.ingested_at
                            ? new Date(r.created_at || r.ingested_at).toLocaleString()
                            : '—'}
                        </td>
                      </tr>
                    );
                  })}
                  {library.length === 0 && (
                    <tr>
                      <td colSpan={4} className="empty">
                        No references filed yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {tab === 'to-read' && (
        <>
          {loading ? (
            <div className="loading">Loading reading list…</div>
          ) : (
            <div className="table-scroll">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Source</th>
                    <th>Reason</th>
                    <th>Demoted</th>
                  </tr>
                </thead>
                <tbody>
                  {failures.map(f => (
                    <tr key={f.id}>
                      <td>
                        <strong>{f.title || '—'}</strong>
                        {f.description && (
                          <div style={{ fontSize: '0.85em', opacity: 0.7, whiteSpace: 'pre-wrap' }}>
                            {f.description.slice(0, 200)}
                          </div>
                        )}
                      </td>
                      <td className="mono">{f.source_tag || '—'}</td>
                      <td style={{ fontSize: '0.9em' }}>
                        {extractDemoteReason(f.demotion_note) || (
                          <span style={{ opacity: 0.5 }}>—</span>
                        )}
                      </td>
                      <td>
                        {f.last_clarified_at
                          ? new Date(f.last_clarified_at).toLocaleString()
                          : f.updated_at
                          ? new Date(f.updated_at).toLocaleString()
                          : '—'}
                      </td>
                    </tr>
                  ))}
                  {failures.length === 0 && (
                    <tr>
                      <td colSpan={4} className="empty">
                        Nothing here — every reference filed cleanly.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function extractDemoteReason(note: string | null | undefined): string | null {
  if (!note) return null;
  // Format: "[ClarifyFlow @ ref-demote] couldn't file in knowledge service — {reason}. Demoted to @to-read."
  const m = note.match(/—\s*(.+?)\.\s*Demoted/);
  return m ? m[1] : null;
}
