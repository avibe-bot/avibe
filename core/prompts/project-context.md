## Project Context and History
Project lessons, conventions, architecture, workflows, and pointers go to the nearest relevant `AGENTS.md`, which future Agents load early.

`AGENTS.md` is an index, not a log. Keep high-level principles there, point to local detail files when needed, and update by consolidating and abstracting instead of merely appending.

When the missing context is previous Avibe conversation history, make good use of `vibe data query` to search Sessions and Messages by keyword, time, scope, Agent, or run history; start from its `digests` view to recognize a Session by what it is about and where it stands, then read its `transcript` or `turns`. Query it freely: it reaches beyond this Session to the whole platform's data across every Session, Agent, and channel, and it is the clearest, most direct, and rawest source of memory. Do not rely on vague impressions or ask the user to repeat themselves.
