# Avibe Agent core: evaluation summary

Evidence behind [`avibe-agent-core.md`](avibe-agent-core.md), collected 2026-09-03 to 2026-10-02. Spikes ran against
a local stub model server that recorded every request body as the record of what the model saw; no real keys or
user state were used. Sources and commits are in §6.

## 1. Control points

The goals require these eleven control points. Each option was measured against the same matrix: `NATIVE` (a public
primitive does it), `WORKAROUND` (possible with code around the framework), or missing.

| ID | Control point |
| --- | --- |
| C1 | Insert and remove tools while a run is in progress |
| C2 | Full context control: transient rewrite of what the model sees, and permanent compaction |
| C3 | Hooks at every lifecycle point, including deny, rewrite arguments, and end early |
| C4 | Another process steers a running loop |
| C5 | Snapshot a session and fork it into two independent branches |
| C6 | Resume after SIGKILL; memory strategy at 10,000+ messages |
| C7 | Three or four wire protocols, and history conversion when the model changes mid-session |
| C8 | Sub-agents inherit hooks and can be steered and forked |
| C9 | A typed event stream for IM progress bubbles |
| C10 | Cost of the Python boundary: glue code, round-trip latency, Python-side tools |
| C11 | Code and dependencies needed for a minimal coding agent |

| Option | Result | Notes |
| --- | --- | --- |
| Bare Python loop (control) | 11/11 native | 893 lines of core (159 of them cross-protocol conversion), 1,866 total; 24 packages / 51 MB; 0.08 ms to the first in-process event |
| LangGraph / LangChain 1.x `create_agent` | 7 native, 4 workaround (C4, C5, C8, C11) | steer needs a self-built mailbox read in `before_model`; no API to clone a thread; sub-agents come from `deepagents` |
| Pi, driven as the `pi` CLI over RPC | 9/11 native | steer, follow-up, session tree, events are primitives; RPC boundary 0.19 ms, Python tool round trip 59 µs |
| tau (`tau_agent` + `tau_ai`) | 3 native (C7, C9, C10), 8 workaround | no hooks around the model call; the loop ignores a tool's `terminate`; `follow_up()` is consumed before the run ends; tool set fixed at run start; `replace_messages` is memory-only |

## 2. Options not adopted

**LangGraph / LangChain.** The workarounds fall on the axis the goals care most about: steering, cloning, and
sub-agents. It brings thread, checkpoint, and interrupt concepts that overlap Avibe's Sessions, Runs, and question
UI. Messages use an append-only reducer, so permanent deletion needs `RemoveMessage` sentinels. At the version read,
middleware returning `Command(goto/resume)` raises `NotImplementedError`. `deepagents`, which supplies sub-agents,
file tools, and summarization, is pre-1.0, requires Python ≥ 3.11 (Avibe supports 3.10), and pulls in `langsmith` and
two vendor SDKs; the measured footprint with it was 62 packages / 109 MB. In its favor: in-process Python and a formal
1.x stability policy with no breaking release in six months.

**Pi (TypeScript).** In the published `pi-agent-core` 0.84.4, `AgentHarness` is a shell whose methods throw
`HarnessNotImplemented`; the full hook surface exists only one layer up, in the coding agent's extension API, so the
workable form is wrapping the whole CLI. Its control depth is real (above), but the agent's brain would live in a
TypeScript process and every Avibe signal (Memory, Harness events, context decisions) would cross a language
boundary. It has no published versioning policy and shipped nine breaking releases in six months, including a patch
release; governance moved to Earendil Inc. in April 2026; external pull requests are closed by a bot; MCP,
sub-agents, permissions, and sandboxing are deliberately out of scope.

**tau (Python port of Pi).** A healthy codebase (1,853 tests green, mypy strict, ruff clean) but a single-author
teaching project. Vendoring it would keep 5,095 lines, delete 48,400 (the TUI and CLI), and rewrite the 635 lines of
`harness.py` and `loop.py` that are exactly the control plane. Its provider layer has correctness gaps against
`pi-ai`: Gemini usage reported as zero, streaming thinking signatures lost, `redacted_thinking` treated as plain
thinking, refusal and safety stops folded into `stop`, no overflow classification, retries without `Retry-After` or
jitter, single-line SSE parsing. Python 3.10 compatibility needs only 15 PEP 695 aliases changed. Adopted as a source
to port from, not as a dependency.

**Claude Agent SDK.** It reaches Model Hub through `ANTHROPIC_BASE_URL`, but speaks only the Anthropic protocol, has no
API to read or replace the message list, cannot swap the whole tool set per call, and its hook abort signal is
reserved but unimplemented. Its control ceiling is the Claude Code CLI's.

## 3. Cross-provider portability

Switching vendors mid-session keeps the full conversation if the transcript is vendor-neutral and local. Pi's rules
(`pi-ai`, `transform-messages`), adopted in C-1:

- Every assistant message records `origin{provider, api, model}`.
- Same origin: replay thinking and its signature verbatim. Different origin: thinking becomes plain assistant text;
  redacted or encrypted reasoning and Gemini thought signatures are dropped; a signature is never synthesized.
- Tool-call ids are normalized to the target protocol's format, with the same mapping applied to their results.
- A tool call without a result gets a synthetic error result; images become a placeholder for non-vision models.
- Responses API with `store: false`, never `previous_response_id`; compaction runs on the client and produces text.

What cannot move between vendors: provider-private reasoning state (cryptographically bound), sampling behavior, and
prompt cache, which starts cold once after a switch. tau's tests include cross-provider history cases to port. Model
Hub's gateway converts protocols only as a fallback, and reasoning may degrade when it does, so the agent speaks the
primary hop's native protocol and records the served hop as `origin`.

## 4. Context management

Pi, Codex, and OpenCode were read at source level; Claude Code from its documentation.

| | Pi | Codex | OpenCode | Claude Code (docs) |
| --- | --- | --- | --- | --- |
| Trigger | context > window − 16,384 | min(config, 0.9·window); hard cap 0.95 | ≥ window − min(max output, 32K) | ≈967K on 1M windows |
| Kept verbatim | ≈20K-token tail | recent **user** messages only | a 2K–15K token tail | recent messages |
| Cut rule | only at user/assistant messages, never at a tool result | none needed (no tool items kept) | whole turns, or a turn suffix | refuses a single exchange |
| Summarizer | own system prompt + serialized transcript, no tools | same model, full history, no tools | own agent + serialized transcript, no tools | same prompt, tools, and history plus an instruction |
| Tool-output clearing | none built in | none | `prune`: protect the last 2 user turns, newest 40K tool tokens, and `skill`; only if ≥ 20K freed; off by default | "clears older tool outputs first" |
| Re-injected afterwards | system prompt; file lists in the summary | full initial context | system prompt every step; todo list lost (#5934) | CLAUDE.md, plan, git status, up to 5 recent files, invoked skills (5K each, 25K total) |
| Storage | appended `compaction` entry; originals kept | appended `compacted{replacement_history}`; originals kept | appended marker and summary; originals kept | appended boundary; originals kept |

What converged and was adopted: an append-only log with a deterministic projection; a summarized head plus a verbatim
tail; safe cut boundaries; fixed summary headings that preserve exact paths, identifiers, and error text; iterative
merge with an explicit previous-summary slot (OpenCode's rule: the prior summary is discarded, anything not carried
forward is lost, and the conversation wins conflicts); usage plus estimate for token counting; durable instructions
re-rendered on every request.

What failed in practice and the rule adopted in response:

| Failure | Evidence | Rule |
| --- | --- | --- |
| Estimate blind to reasoning payloads; compaction cannot shrink the context and the session wedges | Pi #9409 | count the bytes actually replayed; prefer provider usage |
| Trigger ignores the requested output tokens | Pi #8061 | subtract the real `max_tokens` |
| Summary request re-adds content the model never saw, then overflows | Pi #9602 | serialize from the projection actually sent; exclude reasoning |
| Summarizer overflow is terminal or unbounded | OpenCode errors out; Codex drops the oldest item and retries without limit | per-item caps plus chronological chunk-merge |
| Infinite compaction loops | OpenCode #15533, #27924 | one retry per rung, failure and ineffectiveness breakers |
| Synthetic "What did we do so far?" / "Continue…" user turns | OpenCode #13838, #15533 | no synthetic user turn; the checkpoint is framed as a record |
| Agent forgets the task after compaction | Codex #36712, #43855 | keep a verbatim tail ending at the latest tool results |
| Todo or plan lost | OpenCode #5934 | durable state re-rendered from its own store |
| Every compaction is a full cache miss | OpenCode #25120 | accepted in v1 and recorded per checkpoint; decide on cache-safe summarization from that data |

Token estimate for Chinese, measured with the o200k tokenizer: a 72-character Chinese sentence is 51 tokens;
characters / 4 gives 18 (−65%), UTF-8 bytes / 4 gives 54 (+6%). An English sentence: 39 tokens, both estimates 48.

## 5. Tools

**Pi's surface** (`packages/coding-agent/src/core/tools/`): `read{path, offset, limit}`, `write{path, content}`,
`edit{path, edits[{oldText, newText}]}`, `bash{command, timeout}` by default, with optional `grep`, `find`, `ls` off by
default. Output caps 2,000 lines / 50 KB; `read` keeps the head and returns raw text without line numbers; `bash` keeps
the tail and spills the full output to a temp file; `edit` matches exactly, then after a deterministic normalization
(NFKC, trailing whitespace, quotes, dashes, spaces; no similarity threshold). tau mirrors these names, parameters,
descriptions, and constants; its `edit` lacks the normalized tier and its `bash` buffers the whole output in memory.

**Interactive input and long-running commands.**

| Agent | stdin | Long-running commands |
| --- | --- | --- |
| Pi | closed; an extension's own comment says an agent's interactive command "will fail (which is fine)"; Pi's repository drives TUIs with `tmux send-keys` / `capture-pane` through bash | blocks until exit; no default timeout |
| OpenCode | ignored | 2-minute default timeout; no model-facing background; a TODO plans separate get/wait/cancel tools |
| Claude Code | none | `run_in_background` with an output file and a completion notice; a foreground command past its timeout moves to the background |
| Codex | `exec_command` + `write_stdin` with an optional PTY | a command running past `yield_time_ms` returns a session id; now Codex's only shell tool |

No primary source shows that dedicated search, planning, or LSP tools beat shell commands with model and prompt held
constant. Terminal-Bench's reference agent, Terminus, uses only a tmux pane: keystrokes with a wait duration, then the
captured screen.

**Portable job handle prototype** (macOS arm64, Python 3.13): the command runs under a one-line `sh` wrapper in its own
session, output goes to a file, and the wrapper writes the exit code atomically.

- The spawner was SIGKILLed right after start; another process saw the job running, waited on the handle, and got exit
  code 3 and the output tail.
- Killing by handle ended the whole group: four processes (wrapper, shell, two background sleeps) before, none after.
- Overhead: 1.8 ms per command directly, 6.4 ms through the wrapper (50 runs).

**tmux as the job base**, measured with Avibe's vendored tmux 3.6b on a private socket:

| | `sh` wrapper | tmux pane |
| --- | --- | --- |
| Per command | 6.4 ms | 24.6 ms |
| Environment | the caller's | the tmux server's at server start: a caller with `FOO=second` saw `FOO=first` |
| Interactive program | fails at once (closed stdin) | `less` waited for input indefinitely |
| Output | plain text | ANSI sequences and redraws need cleanup |
| Exit code | file | `remain-on-exit` plus `#{pane_dead_status}` (reported 7 correctly) |
| Availability | every platform with an equivalent wrapper | optional dependency; no Windows build |

tmux's distinct value is attaching to and typing into a live terminal, which is what the planned `pty` backend uses.

## 6. Sources

| Project | Revision | License |
| --- | --- | --- |
| Pi (`earendil-works/pi`) | `7fbbd5f`; npm `@earendil-works/pi-*` 0.84.4 | MIT |
| tau (`huggingface/tau`) | 0.4.1 `0a67734` (spike), 0.4.7 `e4eab0d` (tools) | MIT |
| Codex CLI (`openai/codex`) | `b707714` | Apache-2.0 |
| OpenCode (`sst/opencode`) | `a79ecfe` | MIT |
| LangChain 1.3.x, LangGraph 1.2.x, deepagents 0.7.x | PyPI releases of 2026-09 | MIT |
| Claude Code | official documentation, 2026-10 | proprietary; described, not copied |
| Terminal-Bench / Terminus | [tbench.ai/news/terminus](https://www.tbench.ai/news/terminus), [Harbor Terminus-2](https://harborframework.com/docs/agents/terminus-2) | — |

Issues cited: Pi #8061, #9409, #9602, #9904; Codex #7808, #36712, #43855; OpenCode #5934, #13838, #15533, #25120,
#27924.
