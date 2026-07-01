import { useEffect, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

export default function AuditLog() {
  const [entries, setEntries] = useState<any[]>([]);
  const [actorFilter, setActorFilter] = useState('');
  const [actionFilter, setActionFilter] = useState('');
  const [targetTypeFilter, setTargetTypeFilter] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    setLoading(true);
    const params = [
      actorFilter && `actor=${encodeURIComponent(actorFilter)}`,
      actionFilter && `action=${encodeURIComponent(actionFilter)}`,
      targetTypeFilter && `target_type=${encodeURIComponent(targetTypeFilter)}`,
      'limit=200',
    ].filter(Boolean).join('&');

    api.listAuditLog(params)
      .then(data => { setEntries(data || []); setLoading(false); })
      .catch(err => { setError(err); setLoading(false); });
  }, [actorFilter, actionFilter, targetTypeFilter]);

  const uniqueValues = (key: string) =>
    [...new Set(entries.map(e => e[key]).filter(Boolean))].sort();

  const formatDetails = (details: any): string => {
    if (!details) return '';
    if (typeof details === 'string') {
      try { return JSON.stringify(JSON.parse(details), null, 2); } catch { return details; }
    }
    return JSON.stringify(details, null, 2);
  };

  return (
    <div>
      <h1 className="page-title">Audit Log</h1>
      <p className="page-subtitle">System action trail</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="filter-bar">
        <select value={actorFilter} onChange={e => setActorFilter(e.target.value)}>
          <option value="">All actors</option>
          {uniqueValues('actor').map(a => <option key={a} value={a}>{a}</option>)}
        </select>
        <select value={actionFilter} onChange={e => setActionFilter(e.target.value)}>
          <option value="">All actions</option>
          {uniqueValues('action').map(a => <option key={a} value={a}>{a}</option>)}
        </select>
        <select value={targetTypeFilter} onChange={e => setTargetTypeFilter(e.target.value)}>
          <option value="">All target types</option>
          {uniqueValues('target_type').map(t => <option key={t} value={t}>{t}</option>)}
        </select>
        {(actorFilter || actionFilter || targetTypeFilter) && (
          <button
            className="btn btn-sm"
            onClick={() => { setActorFilter(''); setActionFilter(''); setTargetTypeFilter(''); }}
          >Reset</button>
        )}
        <span style={{ marginLeft: 'auto', fontSize: 13, color: 'var(--text-muted)' }}>
          {loading ? 'Loading…' : `${entries.length} ${entries.length === 1 ? 'entry' : 'entries'}`}
        </span>
      </div>

      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Actor</th>
              <th>Action</th>
              <th>Target type</th>
              <th>Target ID</th>
              <th>Details</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(e => {
              const details = formatDetails(e.details);
              const isExpanded = expandedId === e.id;
              return (
                <tr key={e.id}>
                  <td><strong>{e.actor}</strong></td>
                  <td>{e.action}</td>
                  <td>{e.target_type || '—'}</td>
                  <td
                    className="mono"
                    title={e.target_id}
                    style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  >
                    {e.target_id || '—'}
                  </td>
                  <td>
                    {details ? (
                      <>
                        <button className="btn btn-sm" onClick={() => setExpandedId(isExpanded ? null : e.id)}>
                          {isExpanded ? 'Hide' : 'Show'}
                        </button>
                        {isExpanded && <pre className="expandable-details" style={{ whiteSpace: 'pre-wrap', marginTop: 6 }}>{details}</pre>}
                      </>
                    ) : '—'}
                  </td>
                  <td>{e.created_at ? new Date(e.created_at).toLocaleString() : '—'}</td>
                </tr>
              );
            })}
            {!loading && entries.length === 0 && (
              <tr><td colSpan={6} className="empty">No audit entries found</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
