import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

const TIERS = ['fast', 'balanced', 'smart'] as const;

export default function Chat() {
  const [agents, setAgents] = useState<any[]>([]);
  const [threads, setThreads] = useState<any[]>([]);
  const [tierModels, setTierModels] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | string | null>(null);

  const [agentId, setAgentId] = useState('');
  const [tier, setTier] = useState<string>('balanced');
  const [threadId, setThreadId] = useState<string | undefined>(undefined);
  const [msg, setMsg] = useState('');
  const [sending, setSending] = useState(false);
  const [messages, setMessages] = useState<any[]>([]);
  const [threadsOpen, setThreadsOpen] = useState(false);
  const [threadLoading, setThreadLoading] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    Promise.all([
      api.listAgents().catch(() => []),
      api.listThreads().catch(() => []),
      api.getLlmBackend().catch(() => null),
    ]).then(([ag, th, backend]) => {
      setAgents(ag || []);
      setThreads(th || []);
      if (backend?.tiers) {
        const m: Record<string, string> = {};
        for (const t of TIERS) {
          const v = backend.tiers[t];
          if (v) m[t] = typeof v === 'string' ? v : (v.model || v.name || JSON.stringify(v));
        }
        setTierModels(m);
      }
      if (ag && ag.length > 0) setAgentId(ag[0].id);
      setLoading(false);
    }).catch(err => { setError(err); setLoading(false); });
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const getAgentName = (id: string) => agents.find(a => a.id === id)?.name || id;

  async function loadThread(t: any) {
    setThreadsOpen(false);
    setThreadLoading(true);
    setError(null);
    try {
      const history = await api.getThreadHistory(t.thread_id, t.agent_id);
      setMessages(history || []);
      setAgentId(t.agent_id);
      setThreadId(t.thread_id);
    } catch (err: any) {
      setError(err);
    } finally {
      setThreadLoading(false);
    }
  }

  function newConversation() {
    setThreadId(undefined);
    setMessages([]);
    setError(null);
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const text = msg.trim();
    if (!text || !agentId || sending) return;
    setMsg('');
    setSending(true);
    setError(null);
    setMessages(m => [...m, { role: 'user', content: text, created_at: new Date().toISOString() }]);
    try {
      const r = await api.sendMessage(agentId, text, tier, threadId);
      const replyText = r.response ?? r.reply ?? r.message ?? JSON.stringify(r);
      setMessages(m => [...m, { role: 'assistant', content: replyText, created_at: new Date().toISOString() }]);
      if (r.thread_id) setThreadId(r.thread_id);
    } catch (err: any) {
      setError(err);
      setMessages(m => [...m, { role: 'assistant', content: `[error: ${err.message}]`, created_at: new Date().toISOString() }]);
    } finally {
      setSending(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(e as any);
    }
  }

  if (loading) return <div className="loading">Loading chat…</div>;

  return (
    <div>
      <h1 className="page-title">Chat</h1>
      <p className="page-subtitle">Talk to an agent, or browse a past conversation</p>
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      {agents.length === 0 ? (
        <div className="empty">No agents configured yet.</div>
      ) : (
        <>
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="cfg-row" style={{ marginBottom: 0 }}>
              <select value={agentId} onChange={e => { setAgentId(e.target.value); newConversation(); }}>
                {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              <select value={tier} onChange={e => setTier(e.target.value)}>
                {TIERS.map(t => (
                  <option key={t} value={t}>
                    {t}{tierModels[t] ? ` — ${tierModels[t]}` : ''}
                  </option>
                ))}
              </select>
              <button type="button" className="btn btn-sm" onClick={newConversation}>
                New conversation
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setThreadsOpen(o => !o)}>
                {threadsOpen ? 'Hide' : 'Show'} recent threads ({threads.length})
              </button>
              {threadId && (
                <span className="meta mono" style={{ marginLeft: 'auto' }}>thread: {threadId}</span>
              )}
            </div>
          </div>

          {threadsOpen && (
            <div className="card" style={{ marginBottom: 16, maxHeight: 260, overflowY: 'auto' }}>
              {threads.length === 0 && <div className="empty">No threads found</div>}
              {threads.map(t => (
                <div
                  key={`${t.agent_id}-${t.thread_id}`}
                  className={`thread-item ${threadId === t.thread_id && agentId === t.agent_id ? 'active' : ''}`}
                  onClick={() => loadThread(t)}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <strong style={{ fontSize: '0.85rem' }}>{getAgentName(t.agent_id)}</strong>
                    <span className="meta">{t.message_count} msgs</span>
                  </div>
                  <div className="meta" style={{ marginTop: '0.2rem' }}>
                    {t.last_message ? new Date(t.last_message).toLocaleString() : ''}
                  </div>
                  <div className="mono" style={{ fontSize: '0.7rem', color: 'var(--text-muted)', marginTop: '0.1rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t.thread_id}
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="card" style={{ padding: '1rem' }}>
            <div className="chat-messages" style={{ maxHeight: '52vh' }}>
              {threadLoading ? (
                <div className="loading">Loading messages…</div>
              ) : messages.length === 0 ? (
                <div className="empty">Send a message to {getAgentName(agentId)} to start.</div>
              ) : (
                <>
                  {messages.map((m, i) => (
                    <div key={i} className={`chat-bubble ${m.role}`}>
                      <span style={{ wordBreak: 'break-word', whiteSpace: 'pre-wrap' }}>{m.content}</span>
                      <div className="meta" style={{ fontSize: '0.7rem', marginTop: '0.2rem' }}>
                        {m.created_at ? new Date(m.created_at).toLocaleString() : ''}
                      </div>
                    </div>
                  ))}
                  <div ref={messagesEndRef} />
                </>
              )}
            </div>

            <form onSubmit={send} className="chat-input" style={{ marginTop: '0.75rem' }}>
              <textarea
                rows={1}
                value={msg}
                onChange={e => setMsg(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder={`Message ${getAgentName(agentId)}… (Enter to send, Shift+Enter for newline)`}
                disabled={sending}
                style={{ resize: 'none' }}
              />
              <button type="submit" className="btn btn-primary" disabled={sending || !msg.trim() || !agentId}>
                {sending ? 'Sending…' : 'Send'}
              </button>
            </form>
          </div>
        </>
      )}
    </div>
  );
}
