/**
 * API client for AEGIS v2 Core API.
 */

const API_BASE = import.meta.env.VITE_API_URL || '';
const AUTH_KEY = 'aegis_auth';

// Credentials are entered at runtime via the Login page and stored as a
// base64 `user:pass` token — never baked into the JS bundle at build time.
export const setCredentials = (user: string, pass: string): void =>
  localStorage.setItem(AUTH_KEY, btoa(`${user}:${pass}`));
export const clearCredentials = (): void => localStorage.removeItem(AUTH_KEY);
export const hasCredentials = (): boolean => !!localStorage.getItem(AUTH_KEY);
const authToken = (): string | null => localStorage.getItem(AUTH_KEY);

const headers = (): HeadersInit => {
  const token = authToken();
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Basic ${token}` } : {}),
  };
};

// A 401 means the stored creds are wrong/stale (e.g. AEGIS_ADMIN_PASSWORD
// changed) — drop them and bounce back to the login screen.
function onUnauthorized(): never {
  clearCredentials();
  window.location.reload();
  throw new Error('API 401: Unauthorized');
}

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { ...headers(), ...(options?.headers || {}) },
  });
  if (resp.status === 401) onUnauthorized();
  if (!resp.ok) throw new Error(`API ${resp.status}: ${resp.statusText}`);
  // 204 / empty body (e.g. DELETE) → resp.json() would throw on empty input.
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  // Agents
  listAgents: () => apiFetch<any[]>('/api/agents'),
  createAgent: (data: { id: string; name: string; role: string; model_tier?: string; capabilities?: string[]; metadata?: any }) =>
    apiFetch<any>('/api/agents', { method: 'POST', body: JSON.stringify(data) }),
  getAgent: (id: string) => apiFetch<any>(`/api/agents/${id}`),
  getAgentTools: (id: string) => apiFetch<any[]>(`/api/agents/${id}/tools`),
  getAgentOptions: () => apiFetch<any>('/api/agents/meta/options'),
  updateAgent: (id: string, patch: any) =>
    apiFetch<any>(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  draftPersona: (id: string, description: string) =>
    apiFetch<any>(`/api/agents/${id}/draft`, { method: 'POST', body: JSON.stringify({ description }) }),
  getPersonality: (id: string) =>
    apiFetch<Record<string, string>>(`/api/admin/agents/${id}/personality`),
  putPersonality: (id: string, kinds: Record<string, string>) =>
    apiFetch<Record<string, string>>(`/api/admin/agents/${id}/personality`, {
      method: 'PUT', body: JSON.stringify(kinds),
    }),

  // Resources (v3 — connectors, runbooks, repositories, etc.)
  listResources: (kind?: string) =>
    apiFetch<any[]>(`/api/admin/resources${kind ? `?kind=${kind}` : ''}`),
  createResource: (data: any) =>
    apiFetch<any>('/api/admin/resources', { method: 'POST', body: JSON.stringify(data) }),
  updateResource: (id: string, data: any) =>
    apiFetch<any>(`/api/admin/resources/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteResource: (id: string) =>
    apiFetch<any>(`/api/admin/resources/${id}`, { method: 'DELETE' }),

  // Channels (email / rss / raindrop ingestion — DB-owned, UI-managed)
  listChannels: (kind?: string) =>
    apiFetch<any[]>(`/api/admin/channels${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`),
  createChannel: (data: { kind: string; identifier: string; config?: any; active?: boolean }) =>
    apiFetch<any>('/api/admin/channels', { method: 'POST', body: JSON.stringify(data) }),
  updateChannel: (id: string, patch: { identifier?: string; config?: any; active?: boolean }) =>
    apiFetch<any>(`/api/admin/channels/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
  deleteChannel: (id: string) =>
    apiFetch<any>(`/api/admin/channels/${id}`, { method: 'DELETE' }),

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
    const token = authToken();
    const resp = await fetch(`${API_BASE}/api/knowledge/upload`, {
      method: 'POST',
      headers: token ? { Authorization: `Basic ${token}` } : {},
      body: fd,
    });
    if (resp.status === 401) onUnauthorized();
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

  // Chat
  sendMessage: (agentId: string, message: string, tier?: string, threadId?: string) =>
    apiFetch<any>('/api/chat', {
      method: 'POST',
      body: JSON.stringify({
        agent_id: agentId,
        message,
        ...(tier ? { tier } : {}),
        ...(threadId ? { thread_id: threadId } : {}),
      }),
    }),

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

  // Infrastructure registry (dynamic hosts + provisioning)
  listInfra: () => apiFetch<any[]>('/api/admin/infra'),
  getInfra: (id: string) => apiFetch<any>(`/api/admin/infra/${id}`),
  createInfra: (data: any) =>
    apiFetch<any>('/api/admin/infra', { method: 'POST', body: JSON.stringify(data) }),
  updateInfra: (id: string, data: any) =>
    apiFetch<any>(`/api/admin/infra/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteInfra: (id: string) =>
    apiFetch<any>(`/api/admin/infra/${id}`, { method: 'DELETE' }),
  provisionInfra: (id: string) =>
    apiFetch<any>(`/api/admin/infra/${id}/provision`, { method: 'POST' }),
  infraK8sPods: (id: string, namespace = 'default') =>
    apiFetch<any>(`/api/admin/infra/${id}/k8s/pods?namespace=${namespace}`),
  infraK8sDeployments: (id: string, namespace = 'default') =>
    apiFetch<any>(`/api/admin/infra/${id}/k8s/deployments?namespace=${namespace}`),
  infraK8sPodLogs: (id: string, namespace: string, pod: string, tail = 200) =>
    apiFetch<any>(`/api/admin/infra/${id}/k8s/pods/${namespace}/${pod}/logs?tail=${tail}`),
  infraK8sRestartDeployment: (id: string, namespace: string, name: string) =>
    apiFetch<any>(`/api/admin/infra/${id}/k8s/deployments/${namespace}/${name}/restart`, { method: 'POST' }),

  // System monitoring (AEGIS's own stack status)
  systemStatus: () => apiFetch<any>('/api/admin/system/status'),

  // Flows config (scheduled activities) + Google integrations
  listActivities: () => apiFetch<any[]>('/api/admin/activities'),
  updateActivity: (slug: string, patch: { active?: boolean; schedule_cron?: string; config?: any }) =>
    apiFetch<any>(`/api/admin/activities/${encodeURIComponent(slug)}`, {
      method: 'PATCH', body: JSON.stringify(patch),
    }),
  listGoogleAccounts: () => apiFetch<any[]>('/api/admin/integrations/google'),
  listSocialAccounts: () => apiFetch<any[]>('/api/admin/social/accounts'),
  syncPostizAccounts: () => apiFetch<{ synced: number; skipped_disabled: number }>(
    '/api/admin/social/postiz/sync', { method: 'POST' },
  ),
  listSocialPosts: (days = 14) => apiFetch<any[]>(`/api/admin/social/posts?days=${days}`),
  getIntegrations: () => apiFetch<any[]>('/api/admin/integrations/config'),
  saveIntegration: (key: string, value: string) =>
    apiFetch<any[]>('/api/admin/integrations/config', { method: 'PUT', body: JSON.stringify({ key, value }) }),
  getApiKeyStatus: () => apiFetch<{ configured: boolean; source: string }>('/api/admin/api-key'),
  generateApiKey: () => apiFetch<{ api_key: string; configured: boolean }>('/api/admin/api-key/generate', { method: 'POST' }),
  getGoogleOauth: () => apiFetch<any>('/api/admin/integrations/google-oauth'),
  saveGoogleOauth: (body: { client_id: string; client_secret?: string }) =>
    apiFetch<any>('/api/admin/integrations/google-oauth', { method: 'PUT', body: JSON.stringify(body) }),

  // LLM backend (BYO key + backend)
  getLlmBackend: () => apiFetch<any>('/api/admin/llm-backend'),
  saveLlmBackend: (body: any) =>
    apiFetch<any>('/api/admin/llm-backend', { method: 'PUT', body: JSON.stringify(body) }),
  testLlmBackend: (body: any) =>
    apiFetch<any>('/api/admin/llm-backend/test', { method: 'POST', body: JSON.stringify(body) }),

  // Slack (DB-backed, write-only secrets)
  getSlackConfig: () => apiFetch<any>('/api/admin/slack-config'),
  saveSlackConfig: (body: any) =>
    apiFetch<any>('/api/admin/slack-config', { method: 'PUT', body: JSON.stringify(body) }),

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
  getContentRoutes: () => apiFetch<any>('/api/admin/todoist/content-routes'),
  saveContentRoutes: (body: { routes: any[] }) =>
    apiFetch<any>('/api/admin/todoist/content-routes', { method: 'PUT', body: JSON.stringify(body) }),
  previewContentRoute: (body: { match: string; value: string }) =>
    apiFetch<any>('/api/admin/todoist/content-routes/preview', { method: 'POST', body: JSON.stringify(body) }),
  suggestContentRoute: (body: { examples: string[] }) =>
    apiFetch<any>('/api/admin/todoist/content-routes/suggest', { method: 'POST', body: JSON.stringify(body) }),
  // Workbench: tasks, project picker, and clarify-decision visibility
  todoistTasks: (params?: { project_id?: string; status?: string; assignee?: string; limit?: number }) => {
    const q = new URLSearchParams();
    if (params?.project_id) q.set('project_id', params.project_id);
    if (params?.status) q.set('status', params.status);
    if (params?.assignee) q.set('assignee', params.assignee);
    if (params?.limit) q.set('limit', String(params.limit));
    const qs = q.toString();
    return apiFetch<any[]>(`/api/admin/todoist/tasks${qs ? `?${qs}` : ''}`);
  },
  todoistProjects: () => apiFetch<any[]>('/api/admin/todoist/projects'),
  todoistLabels: () => apiFetch<any[]>('/api/admin/todoist/labels'),
  todoistClarifyLog: (params?: { limit?: number; applied?: boolean }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set('limit', String(params.limit));
    if (params?.applied !== undefined) q.set('applied', String(params.applied));
    const qs = q.toString();
    return apiFetch<any[]>(`/api/admin/todoist/clarify-log${qs ? `?${qs}` : ''}`);
  },
  todoistReclarify: (taskId: string) =>
    apiFetch<any>(`/api/admin/todoist/tasks/${encodeURIComponent(taskId)}/reclarify`, { method: 'POST' }),

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

  // Observability — connector-call stats (used by AgentDetail)
  getConnectorStats: (params?: { connector?: string; agent_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.connector) q.set('connector', params.connector);
    if (params?.agent_id) q.set('agent_id', params.agent_id);
    const qs = q.toString();
    return apiFetch<any>(`/api/observability/connector-stats${qs ? `?${qs}` : ''}`);
  },
};
