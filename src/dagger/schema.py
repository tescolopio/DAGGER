"""Strict, shared models for the control plane."""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProviderType = Literal["claude", "codex", "gemini", "copilot", "agy", "api"]
Provider = ProviderType
CostTier = Literal["flat_subscription", "local_free", "metered_api"]
DiscoverySource = Literal["live", "curated", "fallback", "static"]
ProviderStatusValue = Literal[
    "authenticated",
    "configured",
    "unauthenticated",
    "expired",
    "missing_binary",
    "unreachable",
    "error",
    "api_key_configured",
    "unconfigured",
]
DiscoveryStatusValue = Literal["ok", "fallback", "unverified", "error"]
GraphEventType = Literal[
    "NODE_SPAWNED", "EDGE_TRAVERSED", "DOD_PASSED", "DOD_FAILED",
    "HUMAN_GATE", "COMPLETED", "FAILED", "FALLBACK", "SWARM_POST",
]
KanbanStatus = Literal["Backlog", "Planning", "Running", "DoD Audit", "Review", "Done", "Failed"]


def _id() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=True, validate_assignment=True
    )


class AgentContainer(StrictModel):
    container_id: str = Field(default_factory=_id, min_length=1)
    name: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    role: str = Field(min_length=1)
    provider: Provider
    model: str | None = None
    system_prompt: str = ""
    allowed_tools: list[str] = Field(default_factory=list)
    fallback_providers: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    cost_tier: CostTier = "flat_subscription"
    max_concurrency: int = Field(default=2, ge=1)
    base_url: str | None = None
    api_key_env: str | None = None
    active: bool = True
    use_token_auth: bool = False


ContainerDefinition = AgentContainer


class AvailableModel(StrictModel):
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    display_name: str | None = None
    available: bool = True
    is_llm: bool = True
    context_window: int = Field(default=0, ge=0)
    discovery_source: DiscoverySource = "static"
    last_checked: datetime = Field(default_factory=_now)

    @field_validator("last_checked", mode="before")
    @classmethod
    def parse_last_checked(cls, value: Any) -> Any:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


class EndpointCapability(StrictModel):
    provider: Provider
    model_name: str = Field(min_length=1)
    cost_tier: CostTier = "flat_subscription"
    max_context_tokens: int = Field(default=0, ge=0)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    recommended_roles: list[str] = Field(default_factory=list)
    healthy: bool = True
    available: bool = True
    service_provider: str | None = None
    display_name: str | None = None
    is_llm: bool = True
    discovery_source: DiscoverySource = "static"
    version: str | None = None
    last_checked: datetime = Field(default_factory=_now)

    @field_validator("last_checked", mode="before")
    @classmethod
    def parse_last_checked(cls, value: Any) -> Any:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


class DefinitionOfDone(StrictModel):
    acceptance_criteria: list[str]
    expected_artifacts: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)


class SkillSummary(StrictModel):
    name: str = Field(min_length=1)
    description: str = ""
    source: Literal["dagger_local", "skillforge", "workspace"]
    path: str = Field(min_length=1)
    locations: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    valid: bool = True
    validation_errors: list[str] = Field(default_factory=list)


class MilestoneTask(StrictModel):
    milestone_id: str = Field(default_factory=_id, min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    required_skills: list[str] = Field(default_factory=list)
    dod: DefinitionOfDone
    allowed_write_paths: list[str] = Field(default_factory=list)
    assigned_container_id: str | None = None
    status: str = "pending"
    output_summary: str | None = None


class KanbanTask(StrictModel):
    task_id: str = Field(default_factory=_id, min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    status: KanbanStatus = "Backlog"
    run_id: str | None = None
    milestones: list[MilestoneTask] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def parse_timestamp(cls, value: Any) -> Any:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


class TaskEnvelope(StrictModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    kanban_id: str = Field(min_length=1)
    parent_task_id: str | None = None
    container_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    dod: DefinitionOfDone
    worktree_path: str | None = None
    allowed_write_paths: list[str] = Field(default_factory=list)
    allowed_mcp_tools: list[str] = Field(default_factory=list)
    hop_count: int = Field(default=0, ge=0)
    max_hops: int = Field(default=3, ge=0)


class GraphEvent(StrictModel):
    event_id: str = Field(default_factory=_id, min_length=1)
    timestamp: datetime = Field(default_factory=_now)
    run_id: str = Field(min_length=1)
    kanban_id: str = Field(min_length=1)
    event_type: GraphEventType
    source_node: str | None = None
    target_node: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    container_id: str | None = None
    label: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, value: Any) -> Any:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


class UsageEvent(StrictModel):
    event_id: str = Field(default_factory=_id, min_length=1)
    timestamp: datetime = Field(default_factory=_now)
    run_id: str | None = None
    kanban_id: str | None = None
    container_id: str | None = None
    provider: str = Field(min_length=1)
    model: str | None = None
    cost_tier: CostTier = "flat_subscription"
    duration_ms: int = Field(default=0, ge=0)
    input_tokens_est: int = Field(default=0, ge=0)
    output_tokens_est: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    success: bool = True

    @field_validator("timestamp", mode="before")
    @classmethod
    def parse_timestamp(cls, value: Any) -> Any:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


class ProviderCredentialStatus(StrictModel):
    provider_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    kind: Literal["subscription_cli", "api_key", "local"]
    status: ProviderStatusValue
    detail: str = ""
    supports_login: bool = False
    supports_api_key: bool = False
    supports_base_url: bool = False
    requires_api_key: bool = False
    base_url: str | None = None
    category: Literal["llm", "tool", "local"] = "llm"
    available_models: list[AvailableModel] = Field(default_factory=list)
    available_model_count: int = Field(default=0, ge=0)
    discovery_status: DiscoveryStatusValue = "unverified"
    discovery_error: str | None = None
    last_discovered_at: datetime | None = None

    @field_validator("last_discovered_at", mode="before")
    @classmethod
    def parse_last_discovered_at(cls, value: Any) -> Any:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


CardLogChannel = Literal["dialogue", "stdout", "stderr", "command", "system"]


class CardLogEntry(StrictModel):
    event_type: Literal["CARD_LOG"] = "CARD_LOG"
    log_id: str = Field(default_factory=_id, min_length=1)
    kanban_id: str = Field(min_length=1)
    run_id: str = ""
    milestone_id: str | None = None
    node_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    channel: CardLogChannel
    text: str = ""
    ts: str = Field(default_factory=lambda: _now().isoformat())
