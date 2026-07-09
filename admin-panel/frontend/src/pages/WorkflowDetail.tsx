import { useEffect, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';
import JsonViewer from '../components/JsonViewer';

// Temporal status enum -> our badge classes (badge-completed, badge-running, …).
function normalizeStatus(raw: unknown): string {
  if (raw == null) return 'running';
  return String(raw).replace(/^WORKFLOW_EXECUTION_STATUS_/i, '').toLowerCase();
}

// EVENT_TYPE_WORKFLOW_EXECUTION_STARTED -> "workflow execution started"
function prettyEventType(raw: unknown): string {
  return String(raw ?? '').replace(/^EVENT_TYPE_/i, '').replace(/_/g, ' ').toLowerCase();
}

function fmtDate(iso: unknown): string {
  if (!iso) return '—';
  const d = new Date(String(iso));
  return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
}

function fmtDuration(start: unknown, end: unknown): string | null {
  if (!start || !end) return null;
  const ms = new Date(String(end)).getTime() - new Date(String(start)).getTime();
  if (isNaN(ms) || ms < 0) return null;
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

// Each history event carries its type-specific payload under a single
// `<something>EventAttributes` key — pull it out generically so the timeline
// works for any workflow type without hardcoding event names.
function eventAttrs(ev: any): any {
  if (!ev || typeof ev !== 'object') return null;
  const key = Object.keys(ev).find(k => k.endsWith('EventAttributes'));
  return key ? ev[key] : null;
}

// Temporal returns Payload data as base64 of the raw bytes; for the default
// JSON data converter that's base64(JSON). Decode to something readable, else
// fall back to the raw object. ponytail: assumes the JSON converter (what AEGIS
// uses); a custom codec/encryption would need a codec-server round-trip.
function decodePayloads(container: any): any {
  const arr = container?.payloads;
  if (!Array.isArray(arr)) return container ?? null;
  const out = arr.map((p: any) => {
    const data = p?.data;
    if (typeof data !== 'string') return p;
    try {
      const bin = atob(data);
      const bytes = new Uint8Array(bin.length);
      for (let j = 0; j < bin.length; j++) bytes[j] = bin.charCodeAt(j);
      const text = new TextDecoder().decode(bytes);
      try { return JSON.parse(text); } catch { return text; }
    } catch { return p; }
  });
  return out.length === 1 ? out[0] : out;
}

const EVENT_STYLE = { padding: '6px 0', borderTop: '1px solid var(--border)' };
const SUMMARY_STYLE = { cursor: 'pointer' };

export default function WorkflowDetail() {
  const { id = '' } = useParams();
  const [sp] = useSearchParams();
  const runId = sp.get('run') || undefined;

  const [detail, setDetail] = useState<any>(null);
  const [temporalCfg, setTemporalCfg] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      api.getWorkflow(id, runId),
      api.getTemporalConfig().catch(() => null),
    ])
      .then(([d, t]) => { setDetail(d); setTemporalCfg(t); setLoading(false); })
      .catch(e => { setError(e); setLoading(false); });
  }, [id, runId]);

  if (loading) return <div className="loading">Loading workflow…</div>;

  const info = detail?.describe?.workflowExecutionInfo;
  if (error || detail?.error || !info) {
    return (
      <div>
        <Link to="/workflows" className="back-link">&larr; All workflows</Link>
        <h1 className="page-title">Workflow</h1>
        <ErrorBanner error={error} onDismiss={() => setError(null)} />
        <div className="empty">{detail?.error || 'Workflow not found or Temporal unreachable.'}</div>
      </div>
    );
  }

  const events: any[] = Array.isArray(detail?.history?.history?.events)
    ? detail.history.history.events
    : [];
  const pending: any[] = Array.isArray(detail?.describe?.pendingActivities)
    ? detail.describe.pendingActivities
    : [];

  const wfType = info?.type?.name ?? '(unknown type)';
  const status = normalizeStatus(info?.status);
  const workflowId = info?.execution?.workflowId ?? id;
  const effRunId = info?.execution?.runId ?? runId ?? '';
  const duration = fmtDuration(info?.startTime, info?.closeTime);

  // input from the started event, result/failure from the last event.
  const startedAttrs = eventAttrs(
    events.find(e => String(e?.eventType).endsWith('WORKFLOW_EXECUTION_STARTED')),
  );
  const lastAttrs = eventAttrs(events[events.length - 1]);
  const input = startedAttrs?.input ? decodePayloads(startedAttrs.input) : null;
  const result = lastAttrs?.result ? decodePayloads(lastAttrs.result) : null;
  const failure = lastAttrs?.failure ?? null;

  const uiBase = temporalCfg?.temporal_ui_url
    ? String(temporalCfg.temporal_ui_url).replace(/\/$/, '')
    : null;
  const temporalLink = uiBase && workflowId
    ? `${uiBase}/namespaces/default/workflows/${encodeURIComponent(workflowId)}/${encodeURIComponent(effRunId)}/history`
    : null;

  return (
    <div>
      <Link to="/workflows" className="back-link">&larr; All workflows</Link>
      <h1 className="page-title">{wfType}</h1>
      <p className="page-subtitle">
        <span className={`badge badge-${status}`}>{status}</span>
        {' · '}<span className="mono">{workflowId}</span>
      </p>

      <div className="card" style={{ marginBottom: 12 }}>
        <h3>Summary</h3>
        <p className="meta">started: {fmtDate(info?.startTime)}
          {info?.closeTime && <> · closed: {fmtDate(info.closeTime)}</>}
          {duration && <> · duration: {duration}</>}
        </p>
        <p className="meta" style={{ wordBreak: 'break-word' }}>
          run: <span className="mono">{effRunId || '—'}</span>
          {info?.historyLength != null && <> · {info.historyLength} events</>}
          {temporalLink && <> · <a href={temporalLink} target="_blank" rel="noreferrer">open in Temporal UI →</a></>}
        </p>
      </div>

      {pending.length > 0 && (
        <div className="card" style={{ marginBottom: 12 }}>
          <h3>Pending activities</h3>
          {pending.map((pa: any, i: number) => (
            <details key={pa?.activityId ?? i} style={EVENT_STYLE}>
              <summary style={SUMMARY_STYLE}>
                <span className="badge badge-pending">
                  {normalizeStatus(pa?.state).replace(/^pending_activity_state_/, '')}
                </span>
                {' '}<strong>{pa?.activityType?.name ?? pa?.activityId ?? 'activity'}</strong>
                {pa?.attempt && (
                  <span className="meta"> · attempt {pa.attempt}{pa?.maximumAttempts ? `/${pa.maximumAttempts}` : ''}</span>
                )}
              </summary>
              <JsonViewer data={pa} />
            </details>
          ))}
        </div>
      )}

      {input != null && (
        <div className="card" style={{ marginBottom: 12 }}>
          <h3>Input</h3>
          <JsonViewer data={input} />
        </div>
      )}

      {(result != null || failure != null) && (
        <div className="card" style={{ marginBottom: 12 }}>
          <h3>{failure != null ? 'Failure' : 'Result'}</h3>
          <JsonViewer data={failure != null ? failure : result} />
        </div>
      )}

      <div className="card">
        <h3>Event timeline</h3>
        {events.length === 0 && <div className="empty">No history events.</div>}
        {events.map((ev: any, i: number) => (
          <details key={ev?.eventId ?? i} style={EVENT_STYLE}>
            <summary style={SUMMARY_STYLE}>
              <span className="mono">#{ev?.eventId ?? i}</span>
              {' '}<span className="badge badge-type">{prettyEventType(ev?.eventType)}</span>
              {' '}<span className="meta">{fmtDate(ev?.eventTime)}</span>
            </summary>
            <JsonViewer data={eventAttrs(ev) ?? ev} />
          </details>
        ))}
      </div>
    </div>
  );
}
