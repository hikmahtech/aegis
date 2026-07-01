import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import ErrorBanner from '../components/ErrorBanner';

const TIERS = ['fast', 'balanced', 'smart'] as const;

// Models sometimes wrap their answer as {"response": "..."} — unwrap that common
// shape for display; otherwise show the text verbatim.
function displayContent(raw: string): string {
  if (typeof raw !== 'string') return String(raw ?? '');
  const s = raw.trim();
  if (s.startsWith('{') && s.endsWith('}')) {
    try {
      const o = JSON.parse(s);
      const single = o && typeof o === 'object' ? (o.response ?? o.reply ?? o.answer ?? o.text ?? o.content) : null;
      if (typeof single === 'string') return single;
    } catch { /* not JSON — fall through */ }
  }
  return raw;
}

const initial = (s: string) => (s || '?').trim().charAt(0).toUpperCase();

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
  const [threadLoading, setThreadLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

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

  // Keep the transcript pinned to the newest message.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, sending, threadLoading]);

  // Auto-grow the composer up to a cap.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
  }, [msg]);

  const agent = agents.find(a => a.id === agentId);
  const getAgentName = (id: string) => agents.find(a => a.id === id)?.name || id;

  async function loadThread(t: any) {
    setSidebarOpen(false);
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
    taRef.current?.focus();
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
      setMessages(m => [...m, { role: 'assistant', content: replyText, created_at: new Date().toISOString(), tool_calls: r.tool_calls }]);
      if (r.thread_id) setThreadId(r.thread_id);
    } catch (err: any) {
      setError(err);
      setMessages(m => [...m, { role: 'assistant', content: `[error: ${err.message}]`, created_at: new Date().toISOString(), isError: true }]);
    } finally {
      setSending(false);
      taRef.current?.focus();
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send(e as any);
    }
  }

  if (loading) return <div className="loading">Loading chat…</div>;

  if (agents.length === 0) {
    return (
      <div>
        <h1 className="page-title">Chat</h1>
        <ErrorBanner error={error} onDismiss={() => setError(null)} />
        <div className="empty">No agents configured yet. Create one on the Agents page.</div>
      </div>
    );
  }

  const toolCount = (m: any) => Array.isArray(m.tool_calls) ? m.tool_calls.length : 0;

  return (
    <div className="chat-page">
      <ErrorBanner error={error} onDismiss={() => setError(null)} />

      <div className={`chat-shell ${sidebarOpen ? 'sidebar-open' : ''}`}>
        {/* Sidebar: new chat + recent conversations */}
        <aside className="chat-sidebar">
          <div className="chat-sidebar-head">
            <button className="btn btn-primary" style={{ width: '100%' }} onClick={newConversation}>+ New chat</button>
          </div>
          <div className="chat-thread-list">
            <div className="chat-thread-label">Recent</div>
            {threads.length === 0 && <div className="meta" style={{ padding: '8px 4px' }}>No conversations yet.</div>}
            {threads.map(t => {
              const active = threadId === t.thread_id && agentId === t.agent_id;
              return (
                <button
                  key={`${t.agent_id}-${t.thread_id}`}
                  className={`chat-thread ${active ? 'active' : ''}`}
                  onClick={() => loadThread(t)}
                >
                  <span className="msg-avatar assistant" aria-hidden>{initial(getAgentName(t.agent_id))}</span>
                  <span className="chat-thread-body">
                    <span className="chat-thread-top">
                      <strong>{getAgentName(t.agent_id)}</strong>
                      <span className="meta">{t.message_count ?? ''}{t.message_count ? ' msgs' : ''}</span>
                    </span>
                    <span className="meta chat-thread-time">
                      {t.last_message ? new Date(t.last_message).toLocaleString() : ''}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </aside>

        {/* Main conversation pane */}
        <section className="chat-main">
          <header className="chat-main-head">
            <button className="btn btn-sm chat-sidebar-toggle" onClick={() => setSidebarOpen(o => !o)} aria-label="Conversations">☰</button>
            <span className="msg-avatar assistant" aria-hidden>{initial(agent?.name || agentId)}</span>
            <div className="chat-head-agent">
              <select className="chat-agent-select" value={agentId} onChange={e => { setAgentId(e.target.value); newConversation(); }}>
                {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              {agent?.role && <span className="meta">{agent.role}</span>}
            </div>
            <div className="chat-head-model">
              <span className="meta">Model</span>
              <select value={tier} onChange={e => setTier(e.target.value)} title={tierModels[tier] || tier}>
                {TIERS.map(t => (
                  <option key={t} value={t}>{t}{tierModels[t] ? ` · ${tierModels[t]}` : ''}</option>
                ))}
              </select>
            </div>
          </header>

          <div className="chat-scroll" ref={scrollRef}>
            {threadLoading ? (
              <div className="loading">Loading messages…</div>
            ) : messages.length === 0 ? (
              <div className="chat-empty">
                <span className="msg-avatar assistant" style={{ width: 44, height: 44, fontSize: 18 }} aria-hidden>{initial(agent?.name || agentId)}</span>
                <p><strong>{getAgentName(agentId)}</strong></p>
                <p className="meta">{agent?.role || 'Send a message to start the conversation.'}</p>
              </div>
            ) : (
              messages.map((m, i) => (
                <div key={i} className={`msg-row ${m.role === 'user' ? 'user' : 'assistant'}`}>
                  <span className={`msg-avatar ${m.role === 'user' ? 'user' : 'assistant'}`} aria-hidden>
                    {m.role === 'user' ? 'You'.charAt(0) : initial(agent?.name || agentId)}
                  </span>
                  <div className="msg-content">
                    <div className={`msg-bubble ${m.isError ? 'error' : ''}`}>{displayContent(m.content)}</div>
                    {toolCount(m) > 0 && (
                      <div className="msg-tools">🔧 {toolCount(m)} tool call{toolCount(m) > 1 ? 's' : ''}</div>
                    )}
                    <div className="msg-time">{m.created_at ? new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}</div>
                  </div>
                </div>
              ))
            )}
            {sending && (
              <div className="msg-row assistant">
                <span className="msg-avatar assistant" aria-hidden>{initial(agent?.name || agentId)}</span>
                <div className="msg-content">
                  <div className="msg-bubble"><span className="typing-dots"><span /><span /><span /></span></div>
                </div>
              </div>
            )}
          </div>

          <form onSubmit={send} className="chat-composer">
            <textarea
              ref={taRef}
              rows={1}
              value={msg}
              onChange={e => setMsg(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder={`Message ${getAgentName(agentId)}…`}
              disabled={sending}
            />
            <button type="submit" className="btn btn-primary chat-send" disabled={sending || !msg.trim() || !agentId} aria-label="Send">
              {sending ? '…' : '↑'}
            </button>
          </form>
          <div className="chat-hint meta">Enter to send · Shift+Enter for a new line{threadId ? ` · thread ${threadId.slice(0, 8)}…` : ' · new conversation'}</div>
        </section>
      </div>
    </div>
  );
}
