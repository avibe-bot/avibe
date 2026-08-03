import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import type {
  ApiContextType,
  HarnessRun,
  HarnessSessionSummary,
  HarnessTask,
  HarnessWatch,
  VibeAgentBrief,
} from '../../context/ApiContext';
import {
  DetailSession,
  HealthBadge,
  RunDetail,
  RunTriggerChip,
  TaskDetail,
  TaskKindBadge,
  WatchDetail,
} from './HarnessPage';
import { agentDisplayName, loadHarnessAgentCatalog } from './harnessAgents';
import { harnessEmptyStateKey, harnessTabFromParam } from './harnessTabs';
import { RUN_TYPES, harnessSessionState, runRowTitle, runStatusLabel, runTypeLabel, runTypeOptions } from './harnessRuns';

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const render = (ui: ReactElement) =>
  renderToStaticMarkup(
    <I18nextProvider i18n={i18n}>
      <MemoryRouter>{ui}</MemoryRouter>
    </I18nextProvider>,
  );

const NO_SESSION: HarnessSessionSummary = {
  session_title: null,
  session_platform: null,
  session_scope_kind: null,
  session_label: null,
  session_is_workbench: false,
  session_openable: false,
};

const run = (overrides: Partial<HarnessRun>): HarnessRun =>
  ({
    id: 'run-1',
    status: 'succeeded',
    run_type: 'agent_run',
    message: null,
    prompt: null,
    definition_id: null,
    definition_name: null,
    definition_kind: null,
    definition_deleted: false,
    session_id: null,
    callback_session: null,
    ...NO_SESSION,
    ...overrides,
  }) as HarnessRun;

const watch = (overrides: Partial<HarnessWatch>): HarnessWatch => ({
  id: 'watch-1',
  name: 'CI watcher',
  agent_name: null,
  session_policy: null,
  session_id: null,
  session_key: '',
  command: [],
  shell_command: 'true',
  prefix: null,
  message: null,
  message_payload: null,
  cwd: null,
  mode: 'once',
  timeout_seconds: 0,
  lifetime_timeout_seconds: 0,
  retry_exit_codes: [],
  retry_delay_seconds: 0,
  post_to: null,
  deliver_key: null,
  enabled: false,
  created_at: null,
  updated_at: null,
  last_started_at: null,
  last_finished_at: null,
  retired_at: null,
  last_event_at: null,
  last_error: null,
  last_exit_code: null,
  lifecycle_state: 'paused',
  lifecycle_detail: null,
  next_run_at: null,
  waiting_since: null,
  running_since: null,
  runtime: { running: false },
  process_alive: null,
  ...NO_SESSION,
  ...overrides,
});

// A scheduled task as ``/api/harness/tasks`` serves it: the raw store row, so
// ``metadata`` arrives already decoded and the command columns are
// None-preserving. Defaults describe a *message* task, which is what almost
// every stored row is; a command case overrides shell_command/command.
const task = (overrides: Partial<HarnessTask>): HarnessTask =>
  ({
    id: 'task-1',
    name: null,
    agent_name: null,
    session_policy: null,
    session_id: null,
    session_key: '',
    prompt: '',
    message: '',
    message_payload: null,
    schedule_type: 'cron',
    cron: '17 3 * * *',
    run_at: null,
    timezone: 'UTC',
    post_to: null,
    deliver_key: null,
    enabled: true,
    created_at: null,
    updated_at: null,
    last_run_at: null,
    last_run_id: null,
    last_error: null,
    lifecycle_state: 'waiting',
    lifecycle_detail: null,
    next_run_at: null,
    waiting_since: null,
    running_since: null,
    health: null,
    consecutive_failures: 0,
    recent_failures: 0,
    ...NO_SESSION,
    ...overrides,
  }) as HarnessTask;

// Translation-free stand-in: proves the mappers pick the right key without
// depending on the copy.
const key = (k: string) => k;

describe('harnessTabFromParam', () => {
  it('opens the tab a link names', () => {
    expect(harnessTabFromParam('watches')).toBe('watches');
    expect(harnessTabFromParam('runs')).toBe('runs');
  });

  it('lands a link to a tab that no longer exists on Tasks', () => {
    // ?tab=webhooks is still in bookmarks and in old chat messages. It must
    // open a real tab, not a page with nothing lit.
    expect(harnessTabFromParam('webhooks')).toBe('tasks');
    expect(harnessTabFromParam('')).toBe('tasks');
    expect(harnessTabFromParam(null)).toBe('tasks');
  });
});

describe('harnessEmptyStateKey', () => {
  it.each([
    ['tasks', 'harness.emptyTasks', 'harness.noTaskMatches'],
    ['watches', 'harness.emptyWatches', 'harness.noWatchMatches'],
    ['runs', 'harness.emptyRuns', 'harness.noRunMatches'],
  ] as const)('distinguishes an empty store from an empty %s view', (kind, emptyKey, filteredKey) => {
    expect(harnessEmptyStateKey(kind, false)).toBe(emptyKey);
    expect(harnessEmptyStateKey(kind, true)).toBe(filteredKey);
  });
});

describe('loadHarnessAgentCatalog', () => {
  it('bypasses the read cache and indexes a newly archived Agent by its internal name', async () => {
    const archived = {
      id: 'agent-pm',
      name: '_pm-a1b2',
      display_name: 'pm',
      description: null,
      backend: 'codex',
      model: null,
      reasoning_effort: null,
      enabled: false,
      archived: true,
      archived_at: '2026-07-31T21:00:00Z',
      source: 'custom',
      updated_at: '2026-07-31T21:00:00Z',
    } satisfies VibeAgentBrief;
    let params: Parameters<ApiContextType['listVibeAgents']>[0] = undefined;

    const catalog = await loadHarnessAgentCatalog({
      listVibeAgents: async (nextParams) => {
        params = nextParams;
        return { ok: true, agents: [archived], default_agent_name: 'codex' };
      },
    });

    expect(params).toEqual({ includeDisabled: true, includeArchived: true, cache: false });
    expect(catalog).toEqual({ '_pm-a1b2': archived });
  });
});

describe('runRowTitle', () => {
  it("uses the message's first non-empty line, whitespace collapsed", () => {
    expect(
      runRowTitle({ message: '\n\n  Summarize   yesterday\'s PRs  \nmore detail', prompt: null, definition_name: 'Digest' }, 'Agent run'),
    ).toBe("Summarize yesterday's PRs");
  });

  it('falls back to prompt when the run carries no message', () => {
    expect(runRowTitle({ message: null, prompt: 'check CI', definition_name: 'Digest' }, 'Agent run')).toBe('check CI');
  });

  it('falls back to the originating definition name when there is no text', () => {
    expect(runRowTitle({ message: '   \n  ', prompt: null, definition_name: 'Nightly digest' }, 'Agent run')).toBe(
      'Nightly digest',
    );
  });

  it('falls back to the run-type label when nothing else names the run', () => {
    expect(runRowTitle({ message: null, prompt: null, definition_name: null }, 'Watcher heartbeat')).toBe(
      'Watcher heartbeat',
    );
  });

  it('never slices the title — truncation is the row layout\'s job', () => {
    const long = 'x'.repeat(400);
    expect(runRowTitle({ message: long, prompt: null, definition_name: null }, 'Agent run')).toBe(long);
  });
});

describe('RunDetail title', () => {
  it('bounds a pathological title while preserving the complete message', () => {
    const message = `Fix the parser regression.\\n\\n${'Preserve every trailing constraint. '.repeat(40)}`;
    const html = render(<RunDetail run={run({ message })} />);

    expect(html).toContain('line-clamp-2');
    expect(html).toContain('break-words');
    expect(html).toContain(`title="${message.trim()}"`);
    expect(html).toContain(message);
  });

  it('uses the archived Agent display name instead of its routing name', () => {
    const agent: VibeAgentBrief = {
      id: 'agent-pm',
      name: '_pm-8dd7',
      display_name: 'pm',
      description: null,
      backend: 'codex',
      model: null,
      reasoning_effort: null,
      enabled: false,
      archived: true,
      archived_at: '2026-07-31T00:00:00Z',
      source: 'user',
      updated_at: '2026-07-31T00:00:00Z',
    };
    const html = render(<RunDetail run={run({ agent_name: agent.name })} agent={agent} />);

    expect(agentDisplayName(agent.name, agent)).toBe('pm');
    expect(html).toContain('>pm<');
    expect(html).not.toContain('_pm-8dd7');
  });
});

describe('runTypeLabel / runStatusLabel', () => {
  it('maps known internal values to translation keys', () => {
    expect(runTypeLabel('hook_send', key)).toBe('harness.runType.hook_send');
    expect(runTypeLabel(null, key)).toBe('harness.runType.unknown');
    expect(runStatusLabel('canceled', key)).toBe('harness.runStatus.canceled');
  });

  it('shows an unknown value rather than blanking it out', () => {
    expect(runTypeLabel('brand_new_kind', key)).toBe('brand_new_kind');
    expect(runStatusLabel('weird', key)).toBe('weird');
  });

  it('never leaks a raw run_type into user copy for the types the store writes', () => {
    // Driven off RUN_TYPES rather than a copy of it, so declaring a type
    // without translating it fails here.
    for (const value of RUN_TYPES) {
      expect(i18n.t(`harness.runType.${value}`)).not.toBe(`harness.runType.${value}`);
    }
  });
});

describe('runTypeOptions', () => {
  it('offers a type the ledger holds that the UI has no name for', () => {
    // Otherwise the row shows under All and no filter can isolate it: search
    // skips run_type on purpose, so the selector is the only way in.
    expect(runTypeOptions(['agent_run', 'legacy_import'])).toContain('legacy_import');
    expect(runTypeLabel('legacy_import', key)).toBe('legacy_import');
  });

  it('keeps the declared types in their declared order, extras after', () => {
    const options = runTypeOptions(['zeta', 'alpha', 'agent_run']);
    expect(options.slice(0, RUN_TYPES.length)).toEqual([...RUN_TYPES]);
    expect(options.slice(RUN_TYPES.length)).toEqual(['alpha', 'zeta']);
  });

  it('never duplicates a type, and survives a server that omits the facet', () => {
    expect(runTypeOptions(['agent_run', 'dup', 'dup'])).toEqual([...RUN_TYPES, 'dup']);
    expect(runTypeOptions(undefined)).toEqual([...RUN_TYPES]);
  });

  it('lists webhook, which the scheduler writes and the importer preserves', () => {
    expect(runTypeOptions([])).toContain('webhook');
  });
});

describe('harnessSessionState', () => {
  it('reports workbench, im, deleted, and none distinctly', () => {
    expect(harnessSessionState({ ...NO_SESSION, session_is_workbench: true }, 'sess-1')).toBe('workbench');
    expect(harnessSessionState({ ...NO_SESSION, session_platform: 'slack' }, 'sess-1')).toBe('im');
    expect(harnessSessionState(NO_SESSION, 'sess-gone')).toBe('deleted');
    expect(harnessSessionState(NO_SESSION, null)).toBe('none');
  });
});

describe('DetailSession', () => {
  it('links a workbench session to its chat', () => {
    const html = render(
      <DetailSession
        summary={{
          ...NO_SESSION,
          session_is_workbench: true,
          session_openable: true,
          session_title: 'Weekly digest',
        }}
        sessionId="sess-1"
      />,
    );

    expect(html).toContain('href="/chat/sess-1"');
    expect(html).toContain('Weekly digest');
  });

  it('renders a deleted session as a label, not a link', () => {
    const html = render(<DetailSession summary={NO_SESSION} sessionId="sess-gone" />);

    expect(html).toContain('Session deleted');
    expect(html).toContain('sess-gone');
    expect(html).not.toContain('<a ');
    expect(html).not.toContain('/chat/');
  });

  // Linkability is one predicate now (plan §4.5): /chat/<id> serves an
  // IM-bound session exactly as it serves a workbench one, so the row that
  // names it also opens it. What stays IM-specific is only how it *reads* —
  // platform icon and channel label instead of a session title.
  it('links an IM session while still showing it as IM', () => {
    const html = render(
      <DetailSession
        summary={{ ...NO_SESSION, session_platform: 'slack', session_label: '#ops', session_openable: true }}
        sessionId="sess-2"
      />,
    );

    expect(html).toContain('#ops');
    expect(html).toContain('slack');
    expect(html).toContain('href="/chat/sess-2"');
  });

  it('names a session it must not link without pretending it is gone', () => {
    // The one thing /chat/<id> genuinely refuses: the legacy private_agent_run
    // pseudo-scope. The server clears session_openable; the label stays.
    const html = render(
      <DetailSession
        summary={{ ...NO_SESSION, session_is_workbench: true, session_title: 'Spawned run' }}
        sessionId="sess-3"
      />,
    );

    expect(html).toContain('Spawned run');
    expect(html).not.toContain('<a ');
  });

  it('says so when nothing is bound', () => {
    const html = render(<DetailSession summary={NO_SESSION} sessionId={null} />);

    expect(html).toContain('No bound session');
    expect(html).not.toContain('<a ');
  });
});

describe('HealthBadge', () => {
  // The projection contract: ``unknown`` means health could not be computed, and
  // must not read as a clean bill of health. Rendering it as nothing gave the
  // operator a spotless Harness list at exactly the moment the failure signal
  // was unavailable.
  it('names an unknown health instead of dropping it', () => {
    const label = i18n.t('harness.health.unknown');
    expect(label).not.toBe('harness.health.unknown');

    const html = render(<HealthBadge row={watch({ health: 'unknown', consecutive_failures: 3, recent_failures: 7 })} />);

    expect(html).toContain(`>${label}<`);
    // Muted, not pink or amber: a fault in the reporting path is not a verdict
    // that the definition itself is failing.
    expect(html).toContain('text-muted');
    expect(html).not.toContain('text-pink');
    expect(html).not.toContain('text-amber');
  });

  it('shows no count on an unknown row', () => {
    // The counters come from the same unreadable history, so printing them
    // would put a number on a row whose runs could not be read at all.
    const label = i18n.t('harness.health.unknown');
    const html = render(<HealthBadge row={watch({ health: 'unknown', consecutive_failures: 3, recent_failures: 7 })} />);

    expect(html).toContain(`>${label}<`);
    expect(html).not.toContain(`${label} 3`);
    expect(html).not.toContain(`${label} 7`);
  });

  it('renders nothing for a healthy row', () => {
    // A badge on every passing row is noise; silence here is what makes the
    // unknown badge above worth looking at.
    expect(render(<HealthBadge row={watch({ health: 'healthy', consecutive_failures: 0, recent_failures: 0 })} />)).toBe('');
  });

  it('still counts a failing row', () => {
    const html = render(<HealthBadge row={watch({ health: 'failing', consecutive_failures: 4, recent_failures: 4 })} />);

    expect(html).toContain(`>${i18n.t('harness.health.failing')} 4<`);
    expect(html).toContain('text-pink');
  });
});

describe('TaskKindBadge', () => {
  it('marks a command task, so the list distinguishes it from a message task', () => {
    const label = i18n.t('harness.taskKind.command');
    expect(label).not.toBe('harness.taskKind.command');

    const html = render(<TaskKindBadge row={task({ shell_command: 'make test' })} kind="task" />);

    expect(html).toContain(`>${label}<`);
    // Same chip anatomy as the schedule chip beside it.
    expect(html).toContain('font-mono text-[9px] uppercase');
    // The full command is one hover away even though the chip is two words.
    expect(html).toContain('title="make test"');
  });

  it('marks an argv command task too', () => {
    const html = render(<TaskKindBadge row={task({ command: ['python3', 'scripts/health.py'] })} kind="task" />);

    expect(html).toContain(`>${i18n.t('harness.taskKind.command')}<`);
    expect(html).toContain('title="python3 scripts/health.py"');
  });

  it('renders nothing for a message task', () => {
    // Silence is the signal: a chip on every one of the overwhelming majority
    // is noise, and it is what makes the command chip worth reading.
    expect(render(<TaskKindBadge row={task({ prompt: 'Summarize #ops', message: 'Summarize #ops' })} kind="task" />)).toBe('');
    expect(render(<TaskKindBadge row={task({ command: [] })} kind="task" />)).toBe('');
  });

  it('renders nothing for a watch, which always runs a command', () => {
    expect(render(<TaskKindBadge row={watch({ shell_command: 'wait_pr.py 1140' })} kind="watch" />)).toBe('');
  });
});

describe('TaskDetail command task', () => {
  const commandTask = task({
    shell_command: 'pg_dump mydb | gzip > /backups/db.gz',
    session_id: null,
    last_exit_code: 7,
    timeout_seconds: 900,
    metadata: { on_failure: 'agent' },
  });
  const detail = (value: HarnessTask) =>
    render(<TaskDetail task={value} onToggleEnabled={() => undefined} pending={false} />);

  it('shows the command, the failure policy, the timeout and the last exit code', () => {
    const html = detail(commandTask);

    expect(html).toContain(`>${i18n.t('harness.detail.command')}<`);
    expect(html).toContain('pg_dump mydb | gzip &gt; /backups/db.gz');
    expect(html).toContain(`>${i18n.t('harness.detail.onFailure')}<`);
    expect(html).toContain(`>${i18n.t('harness.onFailure.agent')}<`);
    expect(html).toContain(`>${i18n.t('harness.detail.timeout')}<`);
    expect(html).toContain('900s');
    expect(html).toContain(`>${i18n.t('harness.detail.lastExitCode')}<`);
    expect(html).toContain('>7<');
  });

  it('names the command in the header, instead of the word "Scheduled task"', () => {
    // A command task has no name and no message to fall back to, so the
    // existing title chain landed on the kind label and every unnamed command
    // task in the list read as "Scheduled task".
    const html = detail(commandTask);

    expect(html).toContain('title="pg_dump mydb | gzip &gt; /backups/db.gz"');
    expect(html).not.toContain(`>${i18n.t('harness.kind.task')}<`);
  });

  it('renders a command task with no session without a broken chat link', () => {
    // ``--on-failure none`` stores no session at all. DetailSession already
    // owns the four session states, so this must say so rather than link to
    // /chat/null.
    const html = detail(commandTask);

    expect(html).toContain(i18n.t('harness.detail.sessionNone'));
    expect(html).not.toContain('/chat/');
  });

  it('drops the Message field a command task has nothing to put in', () => {
    const html = detail(commandTask);

    expect(html).not.toContain(`>${i18n.t('harness.detail.message')}<`);
  });

  it('keeps the Message field for a command task that escalates with one', () => {
    const html = detail(task({ shell_command: 'make test', message: 'Tests failed, investigate' }));

    expect(html).toContain(`>${i18n.t('harness.detail.message')}<`);
    expect(html).toContain('Tests failed, investigate');
  });

  it('says "no timeout" rather than "0s", which would read as an instant kill', () => {
    const html = detail(task({ shell_command: 'make test', timeout_seconds: 0 }));

    expect(html).toContain(i18n.t('harness.detail.timeoutNone'));
    expect(html).not.toContain('0s');
  });

  it('states the quiet default when no failure policy is stored', () => {
    const html = detail(task({ shell_command: 'make test' }));

    expect(html).toContain(`>${i18n.t('harness.onFailure.none')}<`);
  });

  it('states the timeout that actually applies when the row stores none', () => {
    // SCT-018. A null ``timeout_seconds`` is not "unlimited" and not "unknown": the
    // executor applies COMMAND_TASK_DEFAULT_TIMEOUT_SECONDS, so a six-hour limit is
    // in force. Hiding the field left the pane silent about the one number that
    // decides whether a long backup is killed mid-run, right next to a policy field
    // that DOES state its default — so the omission read as "no limit".
    const html = detail(task({ shell_command: 'make test' }));

    expect(html).toContain(`>${i18n.t('harness.detail.timeout')}<`);
    expect(html).toContain(i18n.t('harness.detail.timeoutDefault', { duration: '6h' }));
    expect(html).not.toContain(i18n.t('harness.detail.timeoutNone'));

    // A stored limit is the user's own number, and carries no default marker.
    const stored = detail(task({ shell_command: 'make test', timeout_seconds: 900 }));
    expect(stored).toContain('900s');
    expect(stored).not.toContain(i18n.t('harness.detail.timeoutDefault', { duration: '6h' }));
  });

  it('colours a zero exit code as neutral and a non-zero one as a failure', () => {
    expect(detail(task({ shell_command: 'make test', last_exit_code: 0 }))).toContain(
      '<span class="font-mono text-[11px] text-muted">0</span>',
    );
    expect(detail(task({ shell_command: 'make test', last_exit_code: 1 }))).toContain(
      '<span class="font-mono text-[11px] text-pink">1</span>',
    );
  });

  it('leaves a message task rendering exactly as it does today', () => {
    const html = detail(task({ name: 'Nightly digest', prompt: 'Summarize #ops', message: 'Summarize #ops' }));

    expect(html).toContain(`>${i18n.t('harness.detail.message')}<`);
    expect(html).toContain('Summarize #ops');
    // None of the command-only fields appear.
    for (const label of ['harness.detail.command', 'harness.detail.onFailure', 'harness.detail.timeout', 'harness.detail.lastExitCode']) {
      expect(html).not.toContain(`>${i18n.t(label)}<`);
    }
    expect(html).not.toContain(i18n.t('harness.taskKind.command'));
  });
});

describe('RunDetail command run', () => {
  it('renders a command run with no session, and keeps its exit code visible', () => {
    // A command run keeps ``run_type: "scheduled"`` and carries exit_code /
    // stdout / stderr with ``session_id: null`` — nothing on this pane may
    // assume every run has a session.
    const html = render(
      <RunDetail
        run={run({
          run_type: 'scheduled',
          status: 'failed',
          session_id: null,
          exit_code: 7,
          stdout: 'dumping mydb',
          stderr: 'pg_dump: connection refused',
          definition_name: 'Nightly backup',
          definition_kind: 'task',
          definition_id: 'def-9',
          metadata: { command: { shell: null, argv: ['pg_dump', 'my db'] } },
        })}
      />,
    );

    expect(html).toContain(i18n.t('harness.detail.sessionNone'));
    expect(html).not.toContain('href="/chat/');
    expect(html).toContain('exit_code 7');
    expect(html).toContain('pg_dump: connection refused');
    // Command runs reuse the existing scheduled run-type label; no new key.
    expect(html).toContain(i18n.t('harness.runType.scheduled'));
    // SCT-019: what ran comes off the RUN's own snapshot, shell-joined, so a
    // definition edited or deleted after the fire cannot rewrite this pane's answer.
    expect(html).toContain(`>${i18n.t('harness.detail.command')}<`);
    expect(html).toContain("pg_dump &#x27;my db&#x27;");
  });
});

describe('WatchDetail runtime', () => {
  it.each([
    [
      'disabled and stopped',
      watch({ enabled: false, process_alive: false }),
      { runtime: false, line: null, pid: null },
    ],
    [
      'enabled and stopped',
      watch({ enabled: true, process_alive: false }),
      { runtime: true, line: '<span class="text-[12px] text-pink">process exited</span>', pid: null },
    ],
    [
      'disabled and alive',
      watch({ enabled: false, process_alive: true, runtime: { running: true, pid: 4321 } }),
      { runtime: true, line: '<span class="text-[12px] text-foreground">process running</span>', pid: 'pid 4321' },
    ],
  ] as const)('renders %s consistently with the row', (_name, value, expected) => {
    const html = render(<WatchDetail watch={value} onToggleEnabled={() => undefined} pending={false} />);

    expect(html.includes('>Runtime<')).toBe(expected.runtime);
    if (expected.line) expect(html).toContain(expected.line);
    else expect(html).not.toContain('process exited');
    if (expected.pid) expect(html).toContain(expected.pid);
  });
});

describe('RunTriggerChip', () => {
  it('links a live watch back to its definition row', () => {
    const html = render(
      <RunTriggerChip run={run({ definition_id: 'def-1', definition_name: 'CI watcher', definition_kind: 'watch' })} />,
    );

    expect(html).toContain('href="/harness?tab=watches&amp;definition=def-1"');
    expect(html).toContain('CI watcher');
  });

  it('routes a scheduled definition to the tasks tab', () => {
    const html = render(
      <RunTriggerChip run={run({ definition_id: 'def-2', definition_name: 'Nightly', definition_kind: 'task' })} />,
    );

    expect(html).toContain('href="/harness?tab=tasks&amp;definition=def-2"');
  });

  it('keeps naming a deleted definition but stops linking to it', () => {
    const html = render(
      <RunTriggerChip
        run={run({ definition_id: 'def-3', definition_name: 'Old digest', definition_kind: 'task', definition_deleted: true })}
      />,
    );

    expect(html).toContain('Old digest');
    expect(html).toContain('(deleted)');
    expect(html).not.toContain('<a ');
  });

  it('renders nothing for a run with no originating definition', () => {
    expect(render(<RunTriggerChip run={run({})} />)).toBe('');
  });

  it('drops the run anchor so the destination list is not hidden behind a detail panel', () => {
    // The chip means "show me this definition in the list". Carrying ?run
    // forward would re-select the run on arrival, and below `lg` an open
    // selection hides the list — the link would show everything except the
    // thing it promised. (The clicked-row case has no ?run to drop; the
    // HarnessPage effect that consumes ?definition clears the selection.)
    const html = render(
      <RunTriggerChip run={run({ definition_id: 'def-4', definition_name: 'Hourly sync', definition_kind: 'task' })} />,
    );

    expect(html).toContain('definition=def-4');
    expect(html).not.toContain('run=');
  });
});
