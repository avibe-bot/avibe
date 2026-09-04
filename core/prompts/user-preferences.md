## Memory and Project Context
Use the right memory surface: {user_context_routing}; project lessons, conventions, architecture, workflows, and pointers go to the nearest relevant `AGENTS.md`, which future Agents load early.

`AGENTS.md` is an index, not a log. Keep high-level principles there, point to local detail files when needed, and update by consolidating and abstracting instead of merely appending.

A shared user context and preferences file is available at `{preferences_path}`. {preferences_usage}

{update_guidance}
Use the current platform `{platform}` and the user id from the current message metadata to choose the appropriate user section: `{platform}/<user_id>`.
Only record durable, factual, reusable information there.
Keep entries short, deduplicated, and free of secrets unless the user explicitly asks.

When the missing memory is previous Avibe conversation history, use `vibe data query` to recover Sessions and Messages by keyword, time, scope, Agent, or run history instead of relying on memory or asking the user to repeat context.
