import { Fragment, createElement, type ReactNode } from 'react';
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import JsonViewer from '../components/JsonViewer';

// Render Telegram-style HTML (<b>, <i>, <a>, <br>, <code>, <pre>) as React
// nodes via an allowlisted DOM walk — no dangerouslySetInnerHTML, no XSS
// surface even though the prompts come from our own server.
const ALLOWED_TAGS: Record<string, string> = {
  B: 'b', STRONG: 'strong', I: 'i', EM: 'em', U: 'u',
  BR: 'br', CODE: 'code', PRE: 'pre',
};
function renderTelegramHtml(raw: string): ReactNode {
  if (!raw) return null;
  const doc = new DOMParser().parseFromString(`<div>${raw}</div>`, 'text/html');
  const root = doc.body.firstElementChild;
  if (!root) return raw;
  const nodes: ReactNode[] = [];
  walk(root, nodes);
  return <>{nodes}</>;

  function walk(parent: Element, out: ReactNode[]) {
    for (const child of Array.from(parent.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        out.push(child.textContent ?? '');
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      const el = child as Element;
      const key = out.length;
      if (el.tagName === 'A') {
        const hrefAttr = el.getAttribute('href') || '';
        const href = /^https?:/i.test(hrefAttr) ? hrefAttr : null;
        const inner: ReactNode[] = [];
        walk(el, inner);
        out.push(href
          ? <a key={key} href={href} target="_blank" rel="noreferrer noopener">{inner}</a>
          : <Fragment key={key}>{inner}</Fragment>);
        continue;
      }
      const tag = ALLOWED_TAGS[el.tagName];
      if (!tag) {
        out.push(el.textContent ?? '');
        continue;
      }
      if (tag === 'br') {
        out.push(<br key={key} />);
        continue;
      }
      const inner: ReactNode[] = [];
      walk(el, inner);
      out.push(createElement(tag, { key }, ...inner));
    }
  }
}

export default function InteractionDetail() {
  const { id = '' } = useParams();
  const [interaction, setInteraction] = useState<any>(null);
  const [temporalCfg, setTemporalCfg] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [draft, setDraft] = useState('');

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.getInteraction(id),
      api.getTemporalConfig().catch(() => null),
    ])
      .then(([i, t]) => {
        setInteraction(i);
        setTemporalCfg(t);
        if (i?.kind === 'draft_review') setDraft(String(i.options?.draft ?? ''));
        if (i?.kind === 'input') setDraft('');
        setLoading(false);
      })
      .catch(e => { setError(e); setLoading(false); });
  }, [id]);

  async function submit(value: string) {
    setSubmitting(true);
    try {
      await api.resolveInteraction(id, { value });
      const refreshed = await api.getInteraction(id);
      setInteraction(refreshed);
    } catch (e: any) {
      setError(e);
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) return <div className="loading">Loading interaction…</div>;
  if (!interaction) return <p>Interaction not found.</p>;

  const pending = interaction.status === 'pending';
  const temporalLink = temporalCfg?.temporal_ui_url
    ? `${String(temporalCfg.temporal_ui_url).replace(/\/$/, '')}/namespaces/default/workflows/${interaction.flow_run_id}`
    : null;

  return (
    <div>
      <Link to="/interactions" className="back-link">&larr; All interactions</Link>
      <h1 className="page-title">{interaction.kind}</h1>
      <p className="page-subtitle">
        <span className={`badge badge-${interaction.status}`}>{interaction.status}</span>
        {' · '}
        {interaction.agent_id} · {interaction.origin}
      </p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ marginBottom: 12 }}>
        <h3>Prompt</h3>
        <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13 }}>
          {renderTelegramHtml(interaction.prompt || '')}
        </div>
      </div>

      {pending && (
        <div className="card" style={{ marginBottom: 12 }}>
          <h3>Respond</h3>
          {renderActionBody(interaction, draft, setDraft, submit, submitting)}
        </div>
      )}

      {!pending && (
        <div className="card" style={{ marginBottom: 12 }}>
          <h3>Response</h3>
          <p className="meta">Resolved {interaction.resolved_at ? new Date(interaction.resolved_at).toLocaleString() : '—'}</p>
          <JsonViewer data={interaction.response} />
        </div>
      )}

      <div className="card">
        <h3>Metadata</h3>
        <p className="meta" style={{ wordBreak: 'break-word' }}>flow_run_id: <span className="mono">{interaction.flow_run_id}</span>
          {temporalLink && <> · <a href={temporalLink} target="_blank" rel="noreferrer">open in Temporal UI →</a></>}
        </p>
        <p className="meta">created: {new Date(interaction.created_at).toLocaleString()}
          {interaction.timeout_at && <> · timeout: {new Date(interaction.timeout_at).toLocaleString()}</>}
          {' · policy: '}{interaction.timeout_policy}
        </p>
        <h4 style={{ fontSize: 13, marginTop: 12 }}>options</h4>
        <JsonViewer data={interaction.options} />
      </div>
    </div>
  );
}

function renderActionBody(
  i: any,
  draft: string,
  setDraft: (v: string) => void,
  submit: (value: string) => void,
  busy: boolean,
) {
  switch (i.kind) {
    case 'approval':
      return (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn btn-primary" disabled={busy} onClick={() => submit('approved')}>Approve</button>
          <button className="btn" disabled={busy} onClick={() => submit('rejected')}>Reject</button>
        </div>
      );
    case 'ack':
      return (
        <button className="btn btn-primary" disabled={busy} onClick={() => submit('ack')}>Acknowledge</button>
      );
    case 'choice': {
      // Two shapes in the wild:
      //   1. options.choices = [{id, label, description?}, …]  (new style)
      //   2. options = {id: label, id: label, …}               (alert path)
      let choices: Array<{ id: string; label: string; description?: string }> = [];
      if (Array.isArray(i.options?.choices)) {
        choices = i.options.choices;
      } else if (i.options && typeof i.options === 'object') {
        choices = Object.entries(i.options)
          .filter(([, v]) => typeof v === 'string')
          .map(([id, label]) => ({ id, label: String(label) }));
      }
      if (choices.length === 0) return <p className="empty">No choices provided in options.</p>;
      return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {choices.map(c => (
            <button key={c.id} className="btn" disabled={busy} onClick={() => submit(c.id)}>
              <strong>{c.label}</strong>{c.description ? ` — ${c.description}` : ''}
            </button>
          ))}
        </div>
      );
    }
    case 'input':
    case 'draft_review': {
      return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            rows={i.kind === 'draft_review' ? 12 : 5}
            style={{ width: '100%', fontFamily: 'inherit' }}
            placeholder={i.kind === 'input' ? 'Type response…' : 'Edit draft…'}
          />
          <div>
            <button className="btn btn-primary" disabled={busy || !draft.trim()} onClick={() => submit(draft)}>
              {busy ? 'Submitting…' : 'Submit'}
            </button>
          </div>
        </div>
      );
    }
    default:
      return <p className="empty">Unknown kind: <code>{i.kind}</code></p>;
  }
}
