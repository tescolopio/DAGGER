export type TaskStatus = 'Backlog' | 'Planning' | 'Running' | 'DoD Audit' | 'Review' | 'Done' | 'Failed';
export type GraphEventType = 'NODE_SPAWNED' | 'EDGE_TRAVERSED' | 'DOD_PASSED' | 'DOD_FAILED' | 'HUMAN_GATE' | 'COMPLETED' | 'FAILED' | 'FALLBACK' | 'SWARM_POST';

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
  cost_tier?: string;
  base_url?: string | null;
  strengths?: string[];
  active: boolean;
  [key: string]: unknown;
}

export type DiscoverySource = 'live' | 'curated' | 'fallback' | 'static';
export type ProviderCategory = 'llm' | 'tool' | 'local';
export type DiscoveryStatus = 'ok' | 'fallback' | 'unverified' | 'error';

export interface AvailableModel {
  provider_id: string;
  model_id: string;
  display_name?: string | null;
  available: boolean;
  is_llm: boolean;
  context_window?: number;
  discovery_source?: DiscoverySource | string;
  last_checked?: string | null;
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
  available?: boolean;
  service_provider?: string | null;
  display_name?: string | null;
  is_llm?: boolean;
  discovery_source?: DiscoverySource | string;
}

export interface GraphEventMetadata extends Record<string, unknown> {
  skills?: string[];
  allowed_mcp_tools?: string[];
}

export interface GraphEvent {
  event_id: string;
  timestamp: string;
  run_id: string;
  kanban_id: string;
  event_type: GraphEventType;
  source_node?: string | null;
  target_node: string;
  node_id?: string | null;
  summary?: string;
  provider: string;
  container_id?: string | null;
  label: string;
  metadata: GraphEventMetadata;
}

export type ProviderStatusKind = 'subscription_cli' | 'api_key' | 'local';
export type ProviderAuthStatus =
  | 'authenticated'
  | 'configured'
  | 'unauthenticated'
  | 'expired'
  | 'missing_binary'
  | 'unreachable'
  | 'error'
  | 'api_key_configured'
  | 'unconfigured';

export interface ProviderStatus {
  provider_id: string;
  display_name: string;
  kind: ProviderStatusKind | string;
  status: ProviderAuthStatus | string;
  detail: string;
  supports_login: boolean;
  supports_api_key: boolean;
  supports_base_url?: boolean;
  requires_api_key?: boolean;
  base_url?: string | null;
  category?: ProviderCategory | string;
  available_models?: AvailableModel[];
  available_model_count?: number;
  discovery_status?: DiscoveryStatus | string;
  discovery_error?: string | null;
  last_discovered_at?: string | null;
}

export interface ProviderLoginResponse {
  provider_id: string;
  status: string;
  message: string;
  requires_callback?: boolean;
  auth_url?: string | null;
  device_url?: string | null;
  device_code?: string | null;
  available_models?: AvailableModel[];
  provider_status?: ProviderStatus | null;
}

export interface ProviderCredentialResponse {
  status: string;
  provider: string;
  catalog_count: number;
  available_models?: AvailableModel[];
  available_model_count?: number;
  provider_status?: ProviderStatus | null;
}

export interface RefreshRegistryResponse {
  capabilities?: Capability[];
  providers?: ProviderStatus[];
  models?: AvailableModel[];
  [key: string]: unknown;
}

export interface ModelsResponse {
  models: AvailableModel[];
}

export interface SubscriptionUsage {
  provider: string;
  calls_3h: number;
  calls_24h: number;
  soft_limit_3h: number;
  soft_limit_24h: number;
  utilization_3h: number;
}

export interface DailyTrendPoint {
  date: string;
  cost_usd: number;
  calls: number;
}

export interface MeteredUsage {
  total_cost_usd: number;
  cost_7d_usd: number;
  total_calls: number;
  daily_trend: DailyTrendPoint[];
}

export interface DownshiftSuggestion {
  container_id: string;
  current_model: string;
  suggested_container_id: string;
  suggested_model: string;
  reason: string;
  estimated_savings_pct: number;
}

export interface UsageSummary {
  subscriptions: SubscriptionUsage[];
  metered: MeteredUsage;
  downshift_suggestions: DownshiftSuggestion[];
  total_events: number;
}

export interface RolodexEntry {
  container_id: string;
  provider: string;
  service_provider?: string;
  model: string | null;
  role: string;
  cost_tier: string;
  cost_rank: number;
  skills: string[];
  active: boolean;
  downshift_suggestion?: string | null;
  line: string;
}

export interface RolodexResponse {
  entries: RolodexEntry[];
  text: string;
}

export interface DownshiftResponse {
  status: string;
  container: Agent;
}

export type SkillSource = 'dagger_local' | 'skillforge' | 'workspace';

export interface SkillSummary {
  name: string;
  description: string;
  source: SkillSource | string;
  path: string;
  locations: string[];
  tags: string[];
  allowed_tools: string[];
  valid: boolean;
  validation_errors: string[];
}

export interface SkillsResponse {
  skills: SkillSummary[];
  count: number;
  sources: Record<string, number>;
}

export type CardLogChannel = 'dialogue' | 'stdout' | 'stderr' | 'command' | 'system';

export interface CardLogEntry {
  event_type?: 'CARD_LOG';
  log_id: string;
  kanban_id: string;
  run_id: string;
  milestone_id?: string | null;
  node_id: string;
  provider: string;
  channel: CardLogChannel;
  text: string;
  ts: string;
}

export interface CardLogsResponse {
  task_id: string;
  run_id: string | null;
  logs: CardLogEntry[];
}
