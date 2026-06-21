# Dynamic Routing Design

This is a historical note for the old backend-first routing design. Current
Avibe routing is Agent-first:

- scopes may set `routing.agent_name` to pick a Vibe Agent;
- if a scope does not set an Agent, new sessions follow the global default
  Agent from `state_meta.default_agent_name`;
- the backend is derived from the selected Agent definition;
- scope model, reasoning, and subagent overrides are applied to the selected
  Agent backend;
- existing sessions keep their own `agent_sessions.agent_backend` snapshot so
  native backend conversations resume on the backend that created them.

The old scope backend route field is deprecated and ignored. The SQLite column
may still exist until a future schema cleanup, but it is no longer a routing
source.
