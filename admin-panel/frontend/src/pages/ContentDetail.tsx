import { useEffect, useState } from 'react';
import { useParams, Link } from 'react-router-dom';
import { api } from '../api/client';

export default function ContentDetail() {
  const { id } = useParams<{ id: string }>();
  const [meta, setMeta] = useState<any>(null);
  const [chunks, setChunks] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!id) return;
    Promise.all([
      api.getKnowledgeContent(id),
      api.getKnowledgeContentChunks(id).catch(() => []),
    ])
      .then(([m, c]) => {
        setMeta(m);
        setChunks(c || []);
        setLoading(false);
      })
      .catch(err => { setError(err.message || 'Failed to load content'); setLoading(false); });
  }, [id]);

  if (loading) return <div className="loading">Loading content…</div>;
  if (error) return <div className="error">{error}</div>;
  if (!meta) return <div className="empty">Content not found</div>;

  const title = meta.title || id;
  const tags: string[] = Array.isArray(meta.tags) ? meta.tags : [];

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <Link to="/content">&larr; Content</Link>
      </div>
      <h1 className="page-title" style={{ wordBreak: 'break-word' }}>{title}</h1>
      <p className="page-subtitle mono" style={{ wordBreak: 'break-all' }}>{id}</p>
      {meta.url && (
        <p style={{ marginTop: -4, wordBreak: 'break-all' }}>
          <a href={meta.url} target="_blank" rel="noreferrer">{meta.url}</a>
        </p>
      )}

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="meta" style={{ wordBreak: 'break-word' }}>
          <span>Status: <strong>{meta.status || 'unknown'}</strong></span>
          {' — '}
          <span>Source: {meta.source_type || '—'}</span>
          {' — '}
          <span>Chunks: {meta.chunks_total ?? chunks.length}</span>
          {' — '}
          <span>Triples: {meta.triples_created ?? '—'}</span>
          {meta.entities_resolved != null && <>{' — '}<span>Entities: {meta.entities_resolved}</span></>}
          {meta.ingested_at && <>{' — '}<span>Ingested: {new Date(meta.ingested_at).toLocaleString()}</span></>}
          {meta.error && <><br/><span className="error">{meta.error}</span></>}
        </div>
        {tags.length > 0 && (
          <div style={{ marginTop: 8 }}>
            {tags.map(t => <span key={t} className="badge badge-type" style={{ marginRight: 6 }}>{t}</span>)}
          </div>
        )}
      </div>

      <h2>Chunks ({chunks.length})</h2>
      <div className="card-vertical-list">
        {chunks.map((c, i) => {
          // KS chunk payload uses chunk_index / chunk_text / section_header.
          const index = c.chunk_index ?? c.index ?? i;
          const text = c.chunk_text || c.text || c.content || '';
          const header = c.section_header;
          return (
            <div key={c.id || c.chunk_id || i} className="card">
              <div className="meta">
                <span>#{index}</span>
                {header && <>{' — '}<span><strong>{header}</strong></span></>}
                {c.token_count != null && <>{' — '}<span>{c.token_count} tokens</span></>}
                {(c.char_start != null && c.char_end != null) && (
                  <>{' — '}<span>chars {c.char_start}–{c.char_end}</span></>
                )}
              </div>
              <p style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{text}</p>
            </div>
          );
        })}
        {chunks.length === 0 && (
          <div className="empty">No chunks available</div>
        )}
      </div>
    </div>
  );
}
