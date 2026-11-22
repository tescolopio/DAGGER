"""DAGGER's local-first agent control plane."""

import os

os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

from .db import DatabaseManager
from .schema import (
    AgentContainer,
    ContainerDefinition,
    CostTier,
    DefinitionOfDone,
    EndpointCapability,
    GraphEvent,
    GraphEventType,
    KanbanTask,
    MilestoneTask,
    ProviderType,
    TaskEnvelope,
)

__all__ = [
    "AgentContainer",
    "ContainerDefinition",
    "CostTier",
    "DatabaseManager",
    "DefinitionOfDone",
    "EndpointCapability",
    "GraphEvent",
    "GraphEventType",
    "KanbanTask",
    "MilestoneTask",
    "ProviderType",
    "TaskEnvelope",
]
