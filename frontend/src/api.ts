import type {
  Agent,
  Capability,
  CardLogsResponse,
  DownshiftResponse,
  GraphEvent,
  ModelsResponse,
  ProviderCredentialResponse,
  ProviderLoginResponse,
  ProviderStatus,
  RefreshRegistryResponse,
  RolodexResponse,
  SkillsResponse,
  Task,
  TaskStatus,
  UsageSummary,
} from './types';

function extractErrorMessage(text: string, status: number): string {
  const trimmed = text.trim();
  if (!trimmed) return `Request failed (${status})`;
  try {
    const parsed = JSON.parse(trimmed) as { detail?: unknown; message?: unknown };
    if (typeof parsed.detail === 'string' && parsed.detail.trim()) {
      return parsed.detail.trim();
    }
    if (Array.isArray(parsed.detail) && parsed.detail.length > 0) {
      const parts = parsed.detail
        .map(item => {
          if (typeof item === 'string') return item;
          if (item && typeof item === 'object' && 'msg' in item) {
            const msg = String((item as { msg?: unknown }).msg ?? '').trim();
            const loc = Array.isArray((item as { loc?: unknown }).loc)
              ? ((item as { loc: unknown[] }).loc).map(String).join('.')
              : '';
            return loc && msg ? `${loc}: ${msg}` : msg;
          }
          return '';
        })
        .filter(Boolean);
      if (parts.length > 0) return parts.join('; ');
    }
    if (typeof parsed.message === 'string' && parsed.message.trim()) {
      return parsed.message.trim();
    }
  } catch {
    // Fall back to plain text response body.
  }
  return trimmed;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(extractErrorMessage(text, response.status));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  tasks: () => request<Task[]>('/api/tasks'),
  agents: () => request<Agent[]>('/api/containers'),
  capabilities: () => request<Capability[]>('/api/capabilities'),
  models: (params?: { provider_id?: string; available_only?: boolean; llm_only?: boolean }) => {
    const qs = new URLSearchParams();
    if (params?.provider_id) qs.set('provider_id', params.provider_id);
    if (params?.available_only !== undefined) qs.set('available_only', String(params.available_only));
    if (params?.llm_only !== undefined) qs.set('llm_only', String(params.llm_only));
    const query = qs.toString();
    return request<ModelsResponse>(`/api/models${query ? `?${query}` : ''}`);
  },
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
  refreshRegistry: () => request<RefreshRegistryResponse>('/api/generator/refresh', { method: 'POST' }),
  providerStatus: () => request<{ providers: ProviderStatus[] }>('/api/providers/status'),
  refreshProvider: (providerId: string) =>
    request<ProviderStatus>(`/api/providers/${encodeURIComponent(providerId)}/refresh`, { method: 'POST' }),
  providerLogin: (providerId: string) =>
    request<ProviderLoginResponse>(`/api/providers/${encodeURIComponent(providerId)}/login`, { method: 'POST' }),
  completeProviderLogin: (providerId: string, input: string) =>
    request<ProviderStatus>(`/api/providers/${encodeURIComponent(providerId)}/login/complete`, {
      method: 'POST',
      body: JSON.stringify({ input }),
    }),
  saveProviderCredentials: (provider: string, apiKey?: string, baseUrl?: string) => {
    const body: { provider: string; api_key?: string; base_url?: string } = { provider };
    if (apiKey !== undefined) {
      body.api_key = apiKey.trim();
    }
    if (baseUrl !== undefined) {
      body.base_url = baseUrl.trim();
    }
    return request<ProviderCredentialResponse>('/api/providers/credentials', {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },
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
