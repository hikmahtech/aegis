import { useEffect, useState } from 'react';
import { api } from '../api/client';

export default function Chat() {
  const [threads, setThreads] = useState<any[]>([]);
  const [agents, setAgents] = useState<any[]>([]);
  const [agentFilter, setAgentFilter] = useState('');
  const [selectedThread, setSelectedThread] = useState<any>(null);
  const [messages, setMessages] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [threadLoading, setThreadLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    Promise.all([
      api.listAgents().catch(() => []),
      api.listThreads(agentFilter ? `agent_id=${agentFilter}` : undefined).catch(() => []),
    ]).then(([ag, t]) => {
      setAgents(ag || []);
      setThreads(t || []);
      setLoading(false);
    }).catch(err => { setError(err.message); setLoading(false); });
  }, [agentFilter]);

  const loadThread = (thread: any) => {
    setSelectedThread(thread);
    setThreadLoading(true);
    api.getThreadHistory(thread.thread_id, thread.agent_id)
      .then(msgs => { setMessages(msgs || []); setThreadLoading(false); })
      .catch(err => { setError(err.message); setThreadLoading(false); });
  };

  const getAgentName = (id: string) => agents.find(a => a.id === id)?.name || id;

  if (loading) return <div className="loading">Loading chat history...</div>;
  if (error) return <div className="error">{error}</div>;

  return (
    <div>
      <h1 className="page-title">Chat</h1>
      <p className="page-subtitle">Past conversations with agents</p>

      <div className="filter-bar">
        <select value={agentFilter} onChange={e => setAgentFilter(e.target.value)}>
          <option value="">All Agents</option>
          {agents.map(a => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <span className="meta" style={{ alignSelf: 'center' }}>{threads.length} threads</span>
      </div>

      <div className="chat-history-layout">
        <div className="thread-list">
          {threads.map(t => (
            <div
              key={`${t.agent_id}-${t.thread_id}`}
              className={`thread-item ${selectedThread?.thread_id === t.thread_id && selectedThread?.agent_id === t.agent_id ? 'active' : ''}`}
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
          {threads.length === 0 && <div className="empty">No threads found</div>}
        </div>

        <div className="thread-messages">
          {!selectedThread ? (
            <div className="empty">Select a thread to view messages</div>
          ) : threadLoading ? (
            <div className="loading">Loading messages...</div>
          ) : (
            <div className="chat-messages" style={{ maxHeight: 'none' }}>
              {messages.map(m => (
                <div key={m.id} className={`chat-bubble ${m.role}`}>
                  <span style={{ wordBreak: 'break-word' }}>{m.content}</span>
                  <div className="meta" style={{ fontSize: '0.7rem', marginTop: '0.2rem' }}>
                    {m.created_at ? new Date(m.created_at).toLocaleString() : ''}
                  </div>
                </div>
              ))}
              {messages.length === 0 && <div className="empty">No messages in this thread</div>}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
