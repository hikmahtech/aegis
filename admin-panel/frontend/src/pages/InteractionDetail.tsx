import { Fragment, createElement, type ReactNode } from 'react';
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import JsonViewer from '../components/JsonViewer';

// Render light chat HTML (<b>, <i>, <a>, <br>, <code>, <pre>) as React
// nodes via an allowlisted DOM walk — no dangerouslySetInnerHTML, no XSS
// surface even though the prompts come from our own server.
const ALLOWED_TAGS: Record<string, string> = {
  B: 'b', STRONG: 'strong', I: 'i', EM: 'em', U: 'u',
  BR: 'br', CODE: 'code', PRE: 'pre',
};
function renderChatHtml(raw: string): ReactNode {
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
  const [reason, setReason] = useState('');
  // The 409 body from a drifted persona draft: the document moved between the
  // card being proposed and this approval, so the server refused it and handed
  // back the CURRENT text. Held in state so the operator sees what their
  // approval would have discarded, and can approve again on purpose.
  const [conflict, setConflict] = useState<any>(null);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.getInteraction(id),
      api.getTemporalConfig().catch(() => null),
    ])
      .then(([i, t]) => {
        setInteraction(i);
        setTemporalCfg(t);
        // draft_review carries the document under review in `metadata`
        // (`proposed_doc` — what ProfileReflectionFlow puts there); the older
        // `options.draft` shape is still honoured as a fallback.
        if (i?.kind === 'draft_review') {
          setDraft(String(i.metadata?.proposed_doc ?? i.options?.draft ?? ''));
        }
        if (i?.kind === 'input') setDraft('');
        setReason('');
        setConflict(null);
        setLoading(false);
      })
      .catch(e => { setError(e); setLoading(false); });
  }, [id]);

  // The single POST path. Every card kind builds its own response object here;
  // nothing else in this file talks to the API.
  async function submitPayload(payload: Record<string, unknown>) {
    setSubmitting(true);
    try {
      await api.resolveInteraction(id, payload);
      const refreshed = await api.getInteraction(id);
      setInteraction(refreshed);
      setConflict(null);
    } catch (e: any) {
      // A refused approval is not an error to dismiss — it is a question to
      // answer, so it gets its own panel rather than the red banner.
      if (e?.status === 409 && e?.detail?.error === 'profile_base_drift') setConflict(e.detail);
      else setError(e);
    } finally {
      setSubmitting(false);
    }
  }

  // approval / ack / choice / input — the historical `{value}` shape.
  const submit = (value: string) => submitPayload({ value });

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
          {renderChatHtml(interaction.prompt || '')}
        </div>
      </div>

      {pending && conflict && (
        <div className="card" style={{ marginBottom: 12, borderColor: 'var(--danger)' }}>
          <h3>The document changed since this was proposed</h3>
          <p className="meta">
            This draft was written against <span className="mono">{conflict.proposed_from}</span>,
            but <span className="mono">{conflict.agent_id}</span>&rsquo;s{' '}
            <span className="mono">{conflict.kind}</span> document is now{' '}
            <span className="mono">{conflict.current}</span> — a hand edit, or another approved
            draft, landed while this card was open. Approving replaces the text below in full.
          </p>
          <pre style={{
            whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12,
            maxHeight: 320, overflow: 'auto',
          }}>{String(conflict.current_doc ?? '')}</pre>
          <p className="meta">
            Read it, merge anything worth keeping into the editor below, then approve again — the
            second approval is recorded against the document shown here.
          </p>
        </div>
      )}

      {pending && (
        <div className="card" style={{ marginBottom: 12 }}>
          <h3>Respond</h3>
          {renderActionBody(interaction, {
            draft, setDraft, reason, setReason, submit, submitPayload, conflict,
            busy: submitting,
          })}
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

interface ActionCtx {
  draft: string;
  setDraft: (v: string) => void;
  reason: string;
  setReason: (v: string) => void;
  submit: (value: string) => void;
  submitPayload: (payload: Record<string, unknown>) => void;
  conflict: any;
  busy: boolean;
}

function renderActionBody(i: any, ctx: ActionCtx) {
  const { draft, setDraft, reason, setReason, submit, submitPayload, conflict, busy } = ctx;
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
    case 'input': {
      return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            rows={5}
            style={{ width: '100%', fontFamily: 'inherit' }}
            placeholder="Type response…"
          />
          <div>
            <button className="btn btn-primary" disabled={busy || !draft.trim()} onClick={() => submit(draft)}>
              {busy ? 'Submitting…' : 'Submit'}
            </button>
          </div>
        </div>
      );
    }
    // A whole document proposed by a flow (ProfileReflectionFlow's weekly
    // persona draft, today). The response is NOT the `{value}` shape the other
    // kinds use — the post-resolve activity keys off `action`, and only
    // "approve" writes anything, so a card of this kind cannot be answered by a
    // plain Submit button. The two payloads below are the contract:
    // tests/worker/test_profile_reflection_e2e.py reads them out of THIS file
    // and drives the real resolve route with them, so renaming a key here fails
    // the build's end-to-end guard rather than silently breaking approvals.
    // `base_ack` is the third key: the resolve route refuses an approve whose
    // base document moved while the card was open (409), and only an ack
    // carrying the fingerprint it handed back unlocks the retry. It is empty —
    // and ignored — on the normal, undrifted path.
    case 'draft_review': {
      const proposed = String(i.metadata?.proposed_doc ?? i.options?.draft ?? '');
      const edited = draft !== proposed;
      // Empty until the server has refused an approval because the document
      // moved (409). Echoing the fingerprint it showed us back on the retry is
      // what says "I saw that text and still mean it" — a blind resubmit of
      // the same payload is refused again.
      const baseAck = String(conflict?.current ?? '');
      return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {proposed && (
            <details>
              <summary className="meta" style={{ cursor: 'pointer' }}>
                Proposed document ({proposed.length} chars){edited ? ' · edited below' : ''}
              </summary>
              <pre style={{
                whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12,
                maxHeight: 320, overflow: 'auto', marginTop: 8,
              }}>{proposed}</pre>
            </details>
          )}
          <label className="meta" htmlFor="draft-review-doc">Document to apply on approval</label>
          <textarea
            id="draft-review-doc"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            rows={16}
            style={{ width: '100%', fontFamily: 'monospace', fontSize: 12 }}
            placeholder="Edit the proposed document…"
          />
          <label className="meta" htmlFor="draft-review-reason">Reason (recorded as a lesson; required to reject)</label>
          <input
            id="draft-review-reason"
            value={reason}
            onChange={e => setReason(e.target.value)}
            style={{ width: '100%' }}
            placeholder="Why are you rejecting this?"
          />
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button
              className="btn btn-primary"
              disabled={busy || !draft.trim()}
              onClick={() => submitPayload({ action: 'approve', edited_doc: draft, base_ack: baseAck })}
            >
              {busy ? 'Submitting…' : conflict ? 'Approve anyway & replace' : 'Approve & apply'}
            </button>
            <button
              className="btn"
              disabled={busy || !reason.trim()}
              onClick={() => submitPayload({ action: 'reject', reason: reason })}
            >
              Reject
            </button>
          </div>
        </div>
      );
    }
    default:
      return <p className="empty">Unknown kind: <code>{i.kind}</code></p>;
  }
}
