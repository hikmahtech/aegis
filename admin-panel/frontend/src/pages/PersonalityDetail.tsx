import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

export default function PersonalityDetail() {
  const { id = '' } = useParams();
  const [agent, setAgent] = useState<any>(null);
  const [tools, setTools] = useState<any[]>([]);
  const [threads, setThreads] = useState<any[]>([]);
  const [llmStats, setLlmStats] = useState<any>(null);
  const [connectorStats, setConnectorStats] = useState<any>(null);
  const [runs, setRuns] = useState<any[]>([]);
  const [error, setError] = useState<Error | null>(null);
  const [msg, setMsg] = useState('');
  const [history, setHistory] = useState<{ role: string; content: string }[]>([]);
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [persona, setPersona] = useState({
    name: '', role: '', model_tier: 'balanced', soul: '', operating_notes: '', user_context: '',
  });
  const [savingP, setSavingP] = useState(false);
  const [pmsg, setPmsg] = useState('');
  const [draftDesc, setDraftDesc] = useState('');
  const [drafting, setDrafting] = useState(false);

  async function load() {
    setError(null); setLoading(true);
    try {
      const [a, t, th, st, cs, rr] = await Promise.all([
        api.getAgent(id),
        api.getAgentTools(id).catch(() => []),
        api.listThreads(`agent_id=${id}`).catch(() => []),
        api.getLLMStats(`agent_id=${id}`).catch(() => null),
        api.getConnectorStats({ agent_id: id }).catch(() => null),
        api.listWorkflowRuns({ agent_id: id, limit: 10 }).catch(() => []),
      ]);
      setAgent(a); setTools(t); setThreads(th);
      setLlmStats(st); setConnectorStats(cs); setRuns(rr);
      setPersona({
        name: a.name || '', role: a.role || '', model_tier: a.model_tier || 'balanced',
        soul: a.soul || '', operating_notes: a.operating_notes || '', user_context: a.user_context || '',
      });
    } catch (e: any) { setError(e); }
    finally { setLoading(false); }
  }

  async function savePersona() {
    setSavingP(true); setPmsg(''); setError(null);
    try { await api.updateAgent(id, persona); setPmsg('Saved.'); await load(); }
    catch (e: any) { setError(e); } finally { setSavingP(false); }
  }

  async function draft() {
    if (!draftDesc.trim()) return;
    setDrafting(true); setError(null);
    try {
      const d = await api.draftPersona(id, draftDesc.trim());
      setPersona(p => ({
        ...p,
        soul: d.soul || p.soul,
        operating_notes: d.operating_notes || p.operating_notes,
        user_context: d.user_context || p.user_context,
      }));
      setPmsg('Drafted — review and Save.');
    } catch (e: any) { setError(e); } finally { setDrafting(false); }
  }
  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [id]);

  async function send(e: React.FormEvent) {
    e.preventDefault();
    if (!msg.trim()) return;
    const text = msg;
    setMsg(''); setSending(true);
    setHistory(h => [...h, { role: 'user', content: text }]);
    try {
      const r = await api.sendMessage(id, text);
      setHistory(h => [...h, { role: 'assistant', content: r.response || r.reply || JSON.stringify(r) }]);
    } catch (err: any) {
      setError(err);
      setHistory(h => [...h, { role: 'assistant', content: `[error: ${err.message}]` }]);
    }
    finally { setSending(false); }
  }

  if (loading) return <div className="loading">Loading personality…</div>;
  if (!agent) return <p>Personality not found.</p>;

  return (
    <div>
      <Link to="/personalities" className="back-link">&larr; All personalities</Link>
      <h1 className="page-title">{agent.name}</h1>
      <p className="page-subtitle">{agent.role} — {agent.description}</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className="card" style={{ marginBottom: 16 }}>
        <h3>Persona</h3>
        <div className="cfg-row" style={{ marginBottom: 8 }}>
          <input value={persona.name}
            onChange={e => setPersona({ ...persona, name: e.target.value })} placeholder="name" />
          <input value={persona.role}
            onChange={e => setPersona({ ...persona, role: e.target.value })} placeholder="role" />
          <select value={persona.model_tier} onChange={e => setPersona({ ...persona, model_tier: e.target.value })}>
            <option value="fast">fast</option>
            <option value="balanced">balanced</option>
            <option value="smart">smart</option>
          </select>
        </div>
        <div className="cfg-row" style={{ marginBottom: 8 }}>
          <input value={draftDesc} onChange={e => setDraftDesc(e.target.value)}
            placeholder="Describe this agent in a sentence and let AI draft the persona…" />
          <button className="btn" disabled={drafting || !draftDesc.trim()} onClick={draft}>
            {drafting ? 'Drafting…' : 'Draft with AI'}
          </button>
        </div>
        {([['soul', 'Identity (SOUL)'], ['operating_notes', 'Operational boundaries (AGENTS)'], ['user_context', 'User context (USER)']] as const).map(([k, label]) => (
          <div key={k} style={{ marginBottom: 8 }}>
            <label style={{ fontSize: 13, color: '#aaa' }}>{label}</label>
            <textarea style={{ width: '100%' }} rows={5} value={(persona as any)[k]}
              onChange={e => setPersona({ ...persona, [k]: e.target.value })} />
          </div>
        ))}
        <button className="btn btn-primary" disabled={savingP} onClick={savePersona}>
          {savingP ? 'Saving…' : 'Save persona'}
        </button>
        {pmsg && <span style={{ marginLeft: 10, color: '#4caf50' }}>{pmsg}</span>}
      </div>

      <div className="grid">
        <div className="card">
          <h3>Tools ({tools.length})</h3>
          <ul style={{ paddingLeft: 18, fontSize: 13, maxHeight: 260, overflowY: 'auto' }}>
            {tools.map(t => <li key={t.name}><code>{t.name}</code> — {t.description}</li>)}
            {tools.length === 0 && <li className="empty">No tool set configured.</li>}
          </ul>
        </div>

        <div className="card">
          <h3>LLM spend</h3>
          {llmStats ? (
            <>
              <p>Calls: <strong>{llmStats.total_calls ?? 0}</strong></p>
              <p>Prompt tokens: {Number(llmStats.total_prompt_tokens || 0).toLocaleString()}</p>
              <p>Completion tokens: {Number(llmStats.total_completion_tokens || 0).toLocaleString()}</p>
              <p>Avg latency: {llmStats.avg_latency_ms ?? 0}ms</p>
            </>
          ) : <p>—</p>}
        </div>

        <div className="card">
          <h3>Connector activity</h3>
          {connectorStats ? (
            <>
              <p>Calls: <strong>{connectorStats.total_calls ?? 0}</strong></p>
              <p>Avg latency: {connectorStats.avg_latency_ms ?? 0}ms</p>
              <p>Errors: {connectorStats.error_count ?? 0}</p>
            </>
          ) : <p>—</p>}
        </div>

        <div className="card">
          <h3>Recent workflow runs</h3>
          {runs.length === 0 && <p className="empty">No runs yet.</p>}
          {runs.map(r => (
            <p key={r.run_id} style={{ fontSize: 13, margin: '4px 0' }}>
              <span className={`badge badge-${String(r.status).toLowerCase()}`} style={{ marginRight: 6 }}>{r.status}</span>
              {r.workflow_type}
              <span className="meta" style={{ marginLeft: 6 }}>
                {r.started_at ? new Date(r.started_at).toLocaleString() : ''}
                {r.duration_ms != null ? ` · ${r.duration_ms}ms` : ''}
              </span>
            </p>
          ))}
          {runs.length > 0 && (
            <p style={{ marginTop: 8 }}><Link to="/workflows">View all →</Link></p>
          )}
        </div>

        <div className="card">
          <h3>Recent threads</h3>
          {threads.slice(0, 5).map(t => (
            <p key={t.thread_id} style={{ fontSize: 13 }}>
              <Link to="/chat">{t.thread_id}</Link> · {t.message_count} msgs
            </p>
          ))}
          {threads.length === 0 && <p className="empty">No threads yet.</p>}
        </div>
      </div>

      <h2 style={{ marginTop: 24 }}>Chat</h2>
      <div className="card" style={{ minHeight: 80, marginBottom: 12 }}>
        {history.length === 0 && <p className="empty">Send a message to start.</p>}
        {history.map((m, i) => (
          <div key={i} className={`chat-bubble ${m.role}`} style={{ margin: '6px 0' }}>
            <strong>{m.role}:</strong> <span style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{m.content}</span>
          </div>
        ))}
      </div>
      <form onSubmit={send} className="cfg-row">
        <input
          value={msg}
          onChange={e => setMsg(e.target.value)}
          placeholder={`Message ${agent.name}…`}
        />
        <button type="submit" className="btn btn-primary" disabled={sending || !msg.trim()}>
          {sending ? 'Sending…' : 'Send'}
        </button>
      </form>
    </div>
  );
}
