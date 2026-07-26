import { createInstance } from 'i18next';
import type { ReactElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import type { HarnessRun, HarnessSessionSummary } from '../../context/ApiContext';
import { DetailSession, RunTriggerChip } from './HarnessPage';
import { harnessSessionState, runRowTitle, runStatusLabel, runTypeLabel } from './harnessRuns';

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

// Translation-free stand-in: proves the mappers pick the right key without
// depending on the copy.
const key = (k: string) => k;

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
    for (const value of ['agent_run', 'watch', 'scheduled', 'task_run', 'hook_send', 'watch_runtime']) {
      expect(i18n.t(`harness.runType.${value}`)).not.toBe(`harness.runType.${value}`);
    }
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
        summary={{ ...NO_SESSION, session_is_workbench: true, session_title: 'Weekly digest' }}
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

  it('shows an IM session without linking it', () => {
    const html = render(
      <DetailSession summary={{ ...NO_SESSION, session_platform: 'slack', session_label: '#ops' }} sessionId="sess-2" />,
    );

    expect(html).toContain('#ops');
    expect(html).not.toContain('<a ');
  });

  it('says so when nothing is bound', () => {
    const html = render(<DetailSession summary={NO_SESSION} sessionId={null} />);

    expect(html).toContain('No bound session');
    expect(html).not.toContain('<a ');
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
});
