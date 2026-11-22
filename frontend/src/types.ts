export type TaskStatus = 'Backlog' | 'Planning' | 'Running' | 'DoD Audit' | 'Review' | 'Done' | 'Failed';
export type GraphEventType = 'NODE_SPAWNED' | 'EDGE_TRAVERSED' | 'DOD_PASSED' | 'DOD_FAILED' | 'HUMAN_GATE' | 'COMPLETED' | 'FAILED' | 'FALLBACK';

export interface Task {
  task_id: string;
  title: string;
  description: string;
  status: TaskStatus;
  run_id?: string | null;
  milestones?: { milestone_id: string; title: string; status: string }[];
}

export interface Agent {
  container_id: string;
  name: string;
  role: string;
  provider: string;
  model: string | null;
  strengths?: string[];
  active: boolean;
}

export interface Capability {
  provider: string;
  model_name: string;
  cost_tier: string;
  max_context_tokens: number;
  strengths: string[];
  weaknesses: string[];
  recommended_roles: string[];
  healthy: boolean;
  last_checked?: string | null;
}

export interface GraphEvent {
  event_id: string;
  timestamp: string;
  run_id: string;
  kanban_id: string;
  event_type: GraphEventType;
  source_node?: string | null;
  target_node: string;
  provider: string;
  container_id?: string | null;
  label: string;
  metadata: Record<string, unknown>;
}
