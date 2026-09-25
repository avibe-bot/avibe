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
| Skills (description always loaded, body on demand) | User skill dirs per backend (e.g. `~/.claude/skills`, `~/.codex/skills`; often symlinked to a shared dir — follow links to the real owner), Avibe built-ins under `skills/` in the Avibe repo, project `.agents/skills/` | Owner of that directory; built-ins via Avibe PR |
| Scheduled Task messages | `vibe task list` / `show` | `vibe task update` |
| Watch messages (re-sent on every fire) | `vibe watch list` / `show` | `vibe watch update`; see `background-watch-hook` before re-creating |
| Delegation briefs and callbacks | `agent_runs.message` / `result_text` | The orchestrating Agent's prompt or Skill that writes them |

## Evidence recipes

`vibe data query` is read-only SQLite; `PRAGMA` is not authorized, so sample a
row (`select * from <table> limit 1`) to see columns.

```sql
-- Failure and cancellation hot spots, last 14 days
select agent_name, agent_backend, status, count(*) n
from agent_runs
where created_at > datetime('now','-14 days') and status in ('failed','canceled')
group by 1,2,3 order by n desc;

-- Runs that ended without reporting anything
select id, agent_name, agent_backend, status, created_at
from agent_runs
where status in ('succeeded','completed')
  and coalesce(trim(result_text),'') = ''
  and created_at > datetime('now','-14 days');

-- User corrections, the strongest signal (adjust keywords to the user's language)
select session_id, created_at, substr(content_text,1,200) text
from messages
where author = 'user' and created_at > datetime('now','-14 days')
  and (content_text like '%why did%' or content_text like '%stop%'
       or content_text like '%为什么%' or content_text like '%卡住%' or content_text like '%不要%');

-- Transcript around a hit
select author, type, created_at, substr(content_text,1,400) text
from messages where session_id = '<session>' order by created_at;
```

`vibe runs show <id>` gives one run's prompt, result, and callback state. The
session's backend and model are in `agent_sessions`.
