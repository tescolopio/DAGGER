import type {
  Agent,
  Capability,
  CardLogsResponse,
  DownshiftResponse,
  GraphEvent,
  ProviderCredentialResponse,
  ProviderLoginResponse,
  ProviderStatus,
  RolodexResponse,
  SkillsResponse,
  Task,
  TaskStatus,
  UsageSummary,
} from './types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  tasks: () => request<Task[]>('/api/tasks'),
  agents: () => request<Agent[]>('/api/containers'),
  capabilities: () => request<Capability[]>('/api/capabilities'),
  events: (runId: string) => request<GraphEvent[]>(`/api/runs/${encodeURIComponent(runId)}/events`),
  taskLogs: (taskId: string, runId?: string | null, limit = 500, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (runId) params.set('run_id', runId);
    if (limit) params.set('limit', String(limit));
    const qs = params.toString();
    return request<CardLogsResponse>(`/api/tasks/${encodeURIComponent(taskId)}/logs${qs ? `?${qs}` : ''}`, { signal });
  },
  createTask: (title: string, description: string, autoStart: boolean) =>
    request<Task>('/api/tasks', { method: 'POST', body: JSON.stringify({ title, description, auto_start: autoStart }) }),
  updateTask: (id: string, status: TaskStatus) =>
    request<Task>(`/api/tasks/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
  approve: (id: string) => request<Task>(`/api/tasks/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
  refreshRegistry: () => request<unknown>('/api/generator/refresh', { method: 'POST' }),
  providerStatus: () => request<{ providers: ProviderStatus[] }>('/api/providers/status'),
  providerLogin: (providerId: string) =>
    request<ProviderLoginResponse>(`/api/providers/${encodeURIComponent(providerId)}/login`, { method: 'POST' }),
  completeProviderLogin: (providerId: string, input: string) =>
    request<ProviderStatus>(`/api/providers/${encodeURIComponent(providerId)}/login/complete`, {
      method: 'POST',
      body: JSON.stringify({ input }),
    }),
  saveProviderCredentials: (provider: string, apiKey: string, baseUrl?: string) =>
    request<ProviderCredentialResponse>('/api/providers/credentials', {
      method: 'POST',
      body: JSON.stringify({
        provider,
        api_key: apiKey,
        ...(baseUrl?.trim() ? { base_url: baseUrl.trim() } : {}),
      }),
    }),
  usageSummary: () => request<UsageSummary>('/api/usage/summary'),
  rolodex: () => request<RolodexResponse>('/api/rolodex'),
  skills: () => request<SkillsResponse>('/api/skills'),
  downshiftContainer: (
    containerId: string,
    body?: { target_model?: string; target_container_id?: string },
  ) =>
    request<DownshiftResponse>(`/api/containers/${encodeURIComponent(containerId)}/downshift`, {
      method: 'POST',
      body: JSON.stringify(body ?? {}),
    }),
  toggleAgentActive: (agent: Agent) =>
    request<Agent>('/api/containers', {
      method: 'POST',
      body: JSON.stringify({ ...agent, active: !agent.active }),
    }),
};
