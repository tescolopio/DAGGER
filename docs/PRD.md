# DAGGER product requirements

DAGGER coordinates local subscription CLI agents, OpenAI-compatible local
endpoints, and explicitly configured metered APIs through a LangGraph workflow.
Tasks are tracked on a SQLite Kanban board; milestone definitions of done
describe outcomes, artifacts, and executable verification checks. Failed
checks produce bounded remediation attempts, and completion requires human
approval.

The control plane maintains a capability and container registry, discovers
locally available providers and optionally enriches it with Tavily, and streams
execution events to a live graph. Filesystem handoffs publish completed JSON
artifacts atomically. Provider concurrency is bounded and rate limits trigger
configured fallbacks. Defaults must preserve subscription authentication,
SQLite checkpoint safety, and loopback-only server binding.
