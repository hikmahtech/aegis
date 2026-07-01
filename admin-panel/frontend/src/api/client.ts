/**
 * API client for AEGIS v2 Core API.
 */

const API_BASE = import.meta.env.VITE_API_URL || '';
const API_USER = import.meta.env.VITE_API_USER || 'admin';
const API_PASS = import.meta.env.VITE_API_PASS || 'admin';

const headers = (): HeadersInit => ({
  'Content-Type': 'application/json',
  'Authorization': `Basic ${btoa(`${API_USER}:${API_PASS}`)}`,
});

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { ...headers(), ...(options?.headers || {}) },
  });
  if (!resp.ok) throw new Error(`API ${resp.status}: ${resp.statusText}`);
  return resp.json();
}

export const api = {
  // Health
  health: () => apiFetch<any>('/health'),

  // Agents
  listAgents: () => apiFetch<any[]>('/api/agents'),
  getAgent: (id: string) => apiFetch<any>(`/api/agents/${id}`),
  getAgentTools: (id: string) => apiFetch<any[]>(`/api/agents/${id}/tools`),
  updateAgent: (id: string, patch: any) =>
    apiFetch<any>(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  draftPersona: (id: string, description: string) =>
    apiFetch<any>(`/api/agents/${id}/draft`, { method: 'POST', body: JSON.stringify({ description }) }),

  // Work Management — Projects (used by Resources page to resolve project names)
  listProjects: (areaId?: string, status?: string) => {
    const params = new URLSearchParams();
    if (areaId) params.set('area_id', areaId);
    if (status !== undefined) params.set('status', status);
    const qs = params.toString();
    return apiFetch<any[]>(`/api/work/projects${qs ? `?${qs}` : ''}`);
  },

  // Resources (v3 — connectors, runbooks, repositories, etc.)
  listResources: (kind?: string) =>
    apiFetch<any[]>(`/api/admin/resources${kind ? `?kind=${kind}` : ''}`),
  createResource: (data: any) =>
    apiFetch<any>('/api/admin/resources', { method: 'POST', body: JSON.stringify(data) }),
  updateResource: (id: string, data: any) =>
    apiFetch<any>(`/api/admin/resources/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteResource: (id: string) =>
    apiFetch<any>(`/api/admin/resources/${id}`, { method: 'DELETE' }),

  // Knowledge
  knowledgeAsk: (question: string) =>
    apiFetch<{ answer: string; sources: any[] }>('/api/knowledge/ask', {
      method: 'POST', body: JSON.stringify({ question }),
    }),
  listKnowledgeContent: (limit?: number) =>
    apiFetch<any[]>(`/api/knowledge/content?limit=${limit || 200}`),
  getKnowledgeContent: (id: string) =>
    apiFetch<any>(`/api/knowledge/content/${encodeURIComponent(id)}`),
  getKnowledgeContentChunks: (id: string) =>
    apiFetch<any[]>(`/api/knowledge/content/${encodeURIComponent(id)}/chunks`),
  knowledgeSearch: (q: string, limit = 10) => {
    const params = new URLSearchParams({ q, limit: String(limit) });
    return apiFetch<any[]>(`/api/knowledge/search?${params.toString()}`);
  },
  // Seeding
  knowledgeIngestUrl: (url: string, source_type = 'content', tags?: string[]) =>
    apiFetch<any>('/api/knowledge/ingest', {
      method: 'POST', body: JSON.stringify({ url, source_type, tags }),
    }),
  knowledgeIngestDrive: (folder_id: string, account: string, source_type = 'drive', tags?: string[]) =>
    apiFetch<any>('/api/knowledge/ingest-drive', {
      method: 'POST', body: JSON.stringify({ folder_id, account, source_type, tags }),
    }),
  knowledgeIngestFolder: (path: string, source_type = 'upload', tags?: string[]) =>
    apiFetch<any>('/api/knowledge/ingest-folder', {
      method: 'POST', body: JSON.stringify({ path, source_type, tags }),
    }),
  knowledgeUpload: async (file: File, source_type = 'upload', tags = ''): Promise<any> => {
    // multipart — must NOT set Content-Type (browser sets the boundary).
    const fd = new FormData();
    fd.append('file', file);
    fd.append('source_type', source_type);
    fd.append('tags', tags);
    const resp = await fetch(`${API_BASE}/api/knowledge/upload`, {
      method: 'POST',
      headers: { Authorization: `Basic ${btoa(`${API_USER}:${API_PASS}`)}` },
      body: fd,
    });
    if (!resp.ok) throw new Error(`API ${resp.status}: ${resp.statusText}`);
    return resp.json();
  },

  // References (raphael's library backed by knowledge-service + @to-read failures)
  listReferences: (filters: { limit?: number; source_tag?: string; q?: string } = {}) => {
    const params = new URLSearchParams();
    if (filters.limit) params.set('limit', String(filters.limit));
    if (filters.source_tag) params.set('source_tag', filters.source_tag);
    if (filters.q) params.set('q', filters.q);
    const qs = params.toString();
    return apiFetch<any[]>(`/api/references${qs ? `?${qs}` : ''}`);
  },
  listReferenceFailures: (limit?: number) =>
    apiFetch<any[]>(`/api/references/failures${limit ? `?limit=${limit}` : ''}`),
  getReference: (id: string) =>
    apiFetch<{ content: any; chunks: any[] }>(`/api/references/${encodeURIComponent(id)}`),

  // Chat
  sendMessage: (agentId: string, message: string) =>
    apiFetch<any>('/api/chat', { method: 'POST', body: JSON.stringify({ agent_id: agentId, message }) }),

  // Chat History
  listThreads: (params?: string) => apiFetch<any[]>(`/api/chat/threads${params ? `?${params}` : ''}`),
  getThreadHistory: (threadId: string, agentId?: string) =>
    apiFetch<any[]>(`/api/chat/history?thread_id=${threadId}${agentId ? `&agent_id=${agentId}` : ''}`),

  // Observability (PersonalityDetail uses getLLMStats)
  getLLMStats: (params?: string) => apiFetch<any>(`/api/observability/llm-stats${params ? `?${params}` : ''}`),

  // Audit
  listAuditLog: (params?: string) => apiFetch<any[]>(`/api/audit${params ? `?${params}` : ''}`),

  // Temporal (Overview shows worker config)
  getTemporalConfig: () => apiFetch<any>('/api/temporal/config'),

  // Market
  marketSummary: () => apiFetch<any>('/api/market/summary'),

  // Infrastructure
  infraListServices: (context = 'swarm') =>
    apiFetch<any>(`/api/infra/services?context=${context}`),
  infraInspectService: (name: string, context = 'swarm') =>
    apiFetch<any>(`/api/infra/services/${name}?context=${context}`),
  infraServiceLogs: (name: string, tail = 200, context = 'swarm') =>
    apiFetch<any>(`/api/infra/services/${name}/logs?context=${context}&tail=${tail}`),
  infraRestartService: (name: string, context = 'swarm') =>
    apiFetch<any>(`/api/infra/services/${name}/restart?context=${context}`, { method: 'POST' }),
  infraListPods: (context = 'acme-prod', namespace = 'default') =>
    apiFetch<any>(`/api/infra/pods?context=${context}&namespace=${namespace}`),
  infraPodLogs: (ns: string, name: string, tail = 200, context = 'acme-prod') =>
    apiFetch<any>(`/api/infra/pods/${ns}/${name}/logs?context=${context}&tail=${tail}`),
  infraListDeployments: (context = 'acme-prod', namespace = 'default') =>
    apiFetch<any>(`/api/infra/deployments?context=${context}&namespace=${namespace}`),
  infraListArgocd: (context = 'acme-prod') =>
    apiFetch<any>(`/api/infra/argocd/apps?context=${context}`),
  infraSyncArgocd: (name: string, context = 'acme-prod') =>
    apiFetch<any>(`/api/infra/argocd/apps/${name}/sync?context=${context}`, { method: 'POST' }),

  // Flows config (scheduled activities) + Google integrations
  listActivities: () => apiFetch<any[]>('/api/admin/activities'),
  updateActivity: (slug: string, patch: { active?: boolean; schedule_cron?: string; config?: any }) =>
    apiFetch<any>(`/api/admin/activities/${encodeURIComponent(slug)}`, {
      method: 'PATCH', body: JSON.stringify(patch),
    }),
  listGoogleAccounts: () => apiFetch<any[]>('/api/admin/integrations/google'),
  getIntegrations: () => apiFetch<any[]>('/api/admin/integrations/config'),
  saveIntegration: (key: string, value: string) =>
    apiFetch<any[]>('/api/admin/integrations/config', { method: 'PUT', body: JSON.stringify({ key, value }) }),
  getGoogleOauth: () => apiFetch<any>('/api/admin/integrations/google-oauth'),
  saveGoogleOauth: (body: { client_id: string; client_secret?: string }) =>
    apiFetch<any>('/api/admin/integrations/google-oauth', { method: 'PUT', body: JSON.stringify(body) }),

  // LLM backend (BYO key + backend)
  getLlmBackend: () => apiFetch<any>('/api/admin/llm-backend'),
  saveLlmBackend: (body: any) =>
    apiFetch<any>('/api/admin/llm-backend', { method: 'PUT', body: JSON.stringify(body) }),
  testLlmBackend: (body: any) =>
    apiFetch<any>('/api/admin/llm-backend/test', { method: 'POST', body: JSON.stringify(body) }),

  // Homelab Guardian
  homelabState: () => apiFetch<any>('/api/admin/homelab/state'),
  homelabRunFlow: (flow: string) => apiFetch<any>(`/api/admin/homelab/${flow}/run`, { method: 'POST' }),

  // Todoist sync + outbox (GTD hub)
  todoistState: () => apiFetch<any>('/api/admin/todoist/state'),
  getTodoistConfig: () => apiFetch<any>('/api/admin/todoist/config'),
  saveTodoistConfig: (body: { api_key?: string; projects?: Record<string, string> }) =>
    apiFetch<any>('/api/admin/todoist/config', { method: 'PUT', body: JSON.stringify(body) }),
  getGtdRules: () => apiFetch<any>('/api/admin/todoist/gtd-rules'),
  saveGtdRules: (body: any) =>
    apiFetch<any>('/api/admin/todoist/gtd-rules', { method: 'PUT', body: JSON.stringify(body) }),

  // Money Hygiene (Maou)
  moneyState: () => apiFetch<any>('/api/admin/money/state'),
  moneyDigest: () => apiFetch<any>('/api/admin/money/digest'),
  moneyRunFlow: (flow: string) => apiFetch<any>(`/api/admin/money/${flow}/run`, { method: 'POST' }),

  // Overview / System
  overviewBrief: () => apiFetch<any>('/api/overview/brief'),
  overviewStatus: () => apiFetch<any>('/api/overview/status'),
  systemInfo: () => apiFetch<any>('/api/system/info'),
  listSettings: () => apiFetch<any[]>('/api/settings'),
  updateSetting: (key: string, value: any) =>
    apiFetch<any>(`/api/settings/${encodeURIComponent(key)}`, {
      method: 'PUT', body: JSON.stringify({ value }),
    }),

  // Interactions
  listInteractions: (params?: { agent_id?: string; status?: string; origin?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.agent_id) q.set('agent_id', params.agent_id);
    if (params?.status) q.set('status', params.status);
    if (params?.origin) q.set('origin', params.origin);
    if (params?.limit) q.set('limit', String(params.limit));
    const qs = q.toString();
    return apiFetch<any[]>(`/api/interactions${qs ? `?${qs}` : ''}`);
  },
  getInteraction: (id: string) =>
    apiFetch<any>(`/api/interactions/${id}`),
  resolveInteraction: (id: string, response: Record<string, unknown>) =>
    apiFetch<any>(`/api/interactions/${id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ response }),
    }),

  // Workflows
  listTemporalWorkflows: (pageSize = 30) =>
    apiFetch<any>(`/api/temporal/workflows?page_size=${pageSize}`),
  listWorkflowRuns: (params?: {
    agent_id?: string;
    workflow_type?: string;
    status?: string;
    limit?: number;
    offset?: number;
  }) => {
    const q = new URLSearchParams();
    if (params?.agent_id) q.set('agent_id', params.agent_id);
    if (params?.workflow_type) q.set('workflow_type', params.workflow_type);
    if (params?.status) q.set('status', params.status);
    if (params?.limit) q.set('limit', String(params.limit));
    if (params?.offset != null) q.set('offset', String(params.offset));
    const qs = q.toString();
    return apiFetch<any[]>(`/api/observability/workflow-runs${qs ? `?${qs}` : ''}`);
  },

  // Observability — connector-call reader + stats (used by PersonalityDetail)
  listConnectorCalls: (params?: { connector?: string; agent_id?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.connector) q.set('connector', params.connector);
    if (params?.agent_id) q.set('agent_id', params.agent_id);
    if (params?.limit) q.set('limit', String(params.limit));
    const qs = q.toString();
    return apiFetch<any[]>(`/api/observability/connector-calls${qs ? `?${qs}` : ''}`);
  },
  getConnectorStats: (params?: { connector?: string; agent_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.connector) q.set('connector', params.connector);
    if (params?.agent_id) q.set('agent_id', params.agent_id);
    const qs = q.toString();
    return apiFetch<any>(`/api/observability/connector-stats${qs ? `?${qs}` : ''}`);
  },
};
