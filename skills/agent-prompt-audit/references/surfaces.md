# Prompt surfaces in an Avibe workspace

Where each layer lives, who owns it, and how to change it. Verify paths against
the current machine; they are starting points, not guarantees.

| Layer | Typical location | Owner / edit channel |
| --- | --- | --- |
| Avibe runtime prompt (capabilities, Harness, Skills catalog, quick replies) | Avibe repo `core/prompts/*.md`, assembled by `core/prompt_registry.py` | Avibe repository PR; propose, do not patch locally |
| Global rules | Backend global files such as `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` | If a file is generated or imports other files (`@path` lines, symlinks, a generated-file header), edit the source and regenerate as it documents; otherwise edit directly |
| Project rules | nearest `AGENTS.md` / `CLAUDE.md` chain from the workdir up | The repository's delivery process |
| Agent system prompt, model, effort | `vibe agent show <name> --json` → `agent.system_prompt` | `vibe agent update <name> --system-prompt-file <file>` |
| Backend-native agent definitions | `~/.claude/agents/*.md`, backend config dirs | Edit the file directly |
| Skills (catalog page 1 descriptions injected; other descriptions and bodies on demand) | User skill dirs per backend (e.g. `~/.claude/skills`, `~/.codex/skills`; often symlinked to a shared dir — follow links to the real owner), Avibe built-ins under `skills/` in the Avibe repo, project `.agents/skills/` | Owner of that directory; built-ins via Avibe PR |
| Scheduled Task messages | `vibe task list` / `show` | `vibe task update` |
| Watch messages (re-sent on every fire) | `vibe watch list` / `show` | `vibe watch update`; see `background-watch-hook` before re-creating |
| Delegation briefs and callbacks | `agent_runs.message` / `result_text` | The orchestrating Agent's prompt or Skill that writes them |

## Evidence recipes

`vibe data query` is read-only SQLite; `PRAGMA` is not authorized, so sample a
row (`select * from <table> limit 1`) to see columns. Scope every query to the
audited Agent's sessions so unrelated conversations never enter the evidence:
resolve them first, then filter by `session_id`. Only Agent-bearing run types
(`agent_run`, `scheduled`, `watch`, `webhook`, `hook`, `task_escalation`)
reflect prompt behavior; `hook_send`, `task_run`, and `watch_runtime` are
command executions. Empty `result_text` means silence only on a terminal run;
queued or running rows are in flight unless they are older than the work
should take.

```sql
-- Sessions in scope
select id, agent_backend, model, reasoning_effort, title, last_active_at
from agent_sessions where agent_name = '<agent>'
order by last_active_at desc limit 20;

-- Failed, cancelled, silent, or stuck Agent runs in those sessions, last 14 days
select id, run_type, status, agent_backend, model, created_at,
       coalesce(trim(result_text),'') = '' as no_result
from agent_runs
where session_id in ('<session>', ...)
  and run_type in ('agent_run','scheduled','watch','webhook','hook','task_escalation')
  and created_at > datetime('now','-14 days')
  and (status in ('failed','canceled')
       or (status in ('succeeded','completed') and coalesce(trim(result_text),'') = '')
       or (status in ('queued','running') and created_at < datetime('now','-2 hours')));

-- User corrections in those sessions (adjust keywords to the user's language)
select session_id, created_at, substr(content_text,1,200) text
from messages
where author = 'user' and session_id in ('<session>', ...)
  and created_at > datetime('now','-14 days')
  and (content_text like '%why did%' or content_text like '%stop%'
       or content_text like '%为什么%' or content_text like '%卡住%' or content_text like '%不要%');

-- Transcript around a hit: locate messages, then read the relevant span
select id, author, type, created_at, length(content_text) len,
       substr(content_text,1,200) head
from messages where session_id = '<session>' order by created_at;

select substr(content_text, max(1, instr(content_text,'<phrase>') - 300), 1200) excerpt
from messages where id = '<message>';
```

`vibe runs show <id>` gives one run's prompt, result, and callback state. The
session's backend and model are in `agent_sessions`.
