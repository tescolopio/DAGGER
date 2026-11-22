import type { Agent, Capability, GraphEvent, Task, TaskStatus } from './types';

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
  createTask: (title: string, description: string, autoStart: boolean) =>
    request<Task>('/api/tasks', { method: 'POST', body: JSON.stringify({ title, description, auto_start: autoStart }) }),
  updateTask: (id: string, status: TaskStatus) =>
    request<Task>(`/api/tasks/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
  approve: (id: string) => request<Task>(`/api/tasks/${encodeURIComponent(id)}/approve`, { method: 'POST' }),
  refreshRegistry: () => request<unknown>('/api/generator/refresh', { method: 'POST' }),
};
