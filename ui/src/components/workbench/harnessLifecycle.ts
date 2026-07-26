import { formatElapsed } from '../../lib/agentGraph';

// Pure mappers behind the Harness task/watch rows (plan §4.1–§4.3). Like
// harnessRuns.ts they take ``t`` rather than calling useTranslation, so every
// branch is unit-testable without a router or an i18n provider, and ``now`` is
// injectable so the time humanizers are deterministic in tests.

// ---------------------------------------------------------------------------
// Lifecycle vocabulary
// ---------------------------------------------------------------------------

export type HarnessLifecycleState = 'running' | 'waiting' | 'paused' | 'finished';
export type HarnessLifecycleDetail = 'normal' | 'timeout' | 'error';

// The filter chips, and the lifecycle states each one selects.
//
// Four chips rather than one per state: ``waiting`` and ``running`` answer a
// single question — "is this still live?" — and the row's own dot and second
// line say which of the two it is, exactly as one ``finished`` chip covers
// three different endings. §4.4 also requires the tab badge to equal the
// default view, and a chip per state would leave the landing view (waiting +
// running) matching no chip at all.
//
// MIRROR of ``DEFINITION_STATUS_FILTERS`` in ``storage/background.py``, which
// maps the same names to the same states for the query. Both sides are pinned
// by a test naming the other file, so a one-sided edit fails instead of quietly
// listing rows the chip never counted.
export const DEFINITION_STATUS_FILTER_STATES: Record<string, readonly HarnessLifecycleState[]> = {
  all: [],
  active: ['waiting', 'running'],
  paused: ['paused'],
  finished: ['finished'],
};

export const DEFINITION_STATUS_FILTERS = ['all', 'active', 'paused', 'finished'] as const;

// The landing view: what is working for the user right now. The previous
// default was "enabled only", which read a switch as a state and buried a
// running task among rows that had merely been left switched on.
export const DEFAULT_DEFINITION_STATUS = 'active';

// How many rows a chip lists, from the per-state counts the server sends.
// ``all`` reads ``total`` rather than summing, so a state the server adds later
// still counts toward it here.
export function definitionStatusCount(
  counts: Record<string, number> | null | undefined,
  status: string,
): number {
  const states = DEFINITION_STATUS_FILTER_STATES[status];
  if (!states || states.length === 0) return counts?.total ?? 0;
  return states.reduce((sum, state) => sum + (counts?.[state] ?? 0), 0);
}

// The tab badge (§4.4): how many things are working for the user right now. It
// is the default view's count by construction — reading it off the same table
// is what stops the badge promising rows the landing view excludes.
export function definitionActiveCount(counts: Record<string, number> | null | undefined): number {
  return definitionStatusCount(counts, DEFAULT_DEFINITION_STATUS);
}

// The states a *switched-on* row can be in. Flipping the switch moves a row
// across this line and nowhere else, which is all the optimistic toggle needs
// to know.
const LIVE_STATES: readonly HarnessLifecycleState[] = ['waiting', 'running'];

// Whether a row still belongs under ``status`` once its switch reads
// ``enabled``. Used to drop the detail panel for a row the user just toggled
// out of the current view — leaving it open would show a row the list no
// longer contains.
export function definitionSurvivesToggle(status: string, enabled: boolean): boolean {
  const states = DEFINITION_STATUS_FILTER_STATES[status];
  if (!states || states.length === 0) return true;
  return states.some((state) => LIVE_STATES.includes(state)) === enabled;
}

// Human words for a state. ``finished`` resolves to how it ended, because
// "finished normally", "timed out" and "failed" are three different outcomes
// that used to render as one word ("disabled") — a watch that timed out instead
// of firing never did its job.
export function lifecycleLabel(
  state: string | null | undefined,
  detail: string | null | undefined,
  t: (k: string) => string,
): string {
  if (state === 'finished') return t(`harness.lifecycle.${detail && DETAILS.has(detail) ? detail : 'normal'}`);
  if (state && STATES.has(state)) return t(`harness.lifecycle.${state}`);
  return t('harness.lifecycle.unknown');
}

const STATES = new Set<string>(['running', 'waiting', 'paused', 'finished']);
const DETAILS = new Set<string>(['normal', 'timeout', 'error']);

// ---------------------------------------------------------------------------
// Time, never printed raw (§4.2)
// ---------------------------------------------------------------------------

const pad = (n: number) => String(n).padStart(2, '0');

// Whole calendar days from ``from`` to ``to``, in the reader's local timezone —
// not (ms / 86400000), which calls 23:59 and 00:01 the same day when they are
// two minutes and two dates apart.
function calendarDayDelta(target: Date, now: Date): number {
  const a = Date.UTC(target.getFullYear(), target.getMonth(), target.getDate());
  const b = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((a - b) / 86_400_000);
}

// An ISO instant as a phrase on the reader's own clock: "today 13:35",
// "tomorrow 09:00", "07-30 10:17", "2027-01-01 00:00". Unparseable input comes
// back verbatim rather than as "Invalid Date"; the detail panel still shows the
// full timestamp.
export function humanizeTime(
  iso: string | null | undefined,
  t: (k: string, opts?: any) => string,
  now: number = Date.now(),
): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const nowDate = new Date(now);
  const delta = calendarDayDelta(d, nowDate);
  if (delta === 0) return t('harness.when.today', { time });
  if (delta === 1) return t('harness.when.tomorrow', { time });
  if (delta === -1) return t('harness.when.yesterday', { time });
  const date =
    d.getFullYear() === nowDate.getFullYear()
      ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
      : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return t('harness.when.dateTime', { date, time });
}

// Compact distance between an instant and now, in R1's duration vocabulary
// ("42s" / "7m" / "3h"). Direction is the caller's to name, so one helper
// serves both "waiting 3h" and "next in 20m".
export function humanizeGap(iso: string | null | undefined, now: number = Date.now()): string | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  return formatElapsed(Math.abs(at - now) / 1000);
}

// ---------------------------------------------------------------------------
// Cron, in words
// ---------------------------------------------------------------------------

// Cron's own week numbering: 0 and 7 are both Sunday.
const WEEKDAY_NAMES = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
const WEEKDAY_INDEX: Record<string, number> = WEEKDAY_NAMES.reduce(
  (map, name, index) => ({ ...map, [name]: index }),
  {},
);

function weekdayNumber(token: string): number | null {
  const named = WEEKDAY_INDEX[token.toLowerCase()];
  if (named != null) return named;
  if (!/^\d+$/.test(token)) return null;
  const value = Number(token);
  if (value === 7) return 0;
  return value >= 0 && value <= 6 ? value : null;
}

// "1", "1-5", "1,3,5", "MON-FRI" → the days, ascending and de-duplicated.
// ``null`` for anything else, which sends the whole expression to the raw
// fallback rather than to a guess.
function weekdays(field: string): number[] | null {
  const days = new Set<number>();
  for (const part of field.split(',')) {
    const range = part.split('-');
    if (range.length === 1) {
      const day = weekdayNumber(range[0]);
      if (day == null) return null;
      days.add(day);
      continue;
    }
    if (range.length !== 2) return null;
    const start = weekdayNumber(range[0]);
    const end = weekdayNumber(range[1]);
    if (start == null || end == null) return null;
    // Cron wraps (FRI-MON); walking forward mod 7 covers both directions.
    for (let day = start; ; day = (day + 1) % 7) {
      days.add(day);
      if (day === end) break;
    }
  }
  return days.size ? [...days].sort((a, b) => a - b) : null;
}

function clockTime(hour: string, minute: string): string | null {
  if (!/^\d{1,2}$/.test(hour) || !/^\d{1,2}$/.test(minute)) return null;
  const h = Number(hour);
  const m = Number(minute);
  if (h > 23 || m > 59) return null;
  return `${pad(h)}:${pad(m)}`;
}

// A cron expression as a phrase: "17 10 * * *" → "every day at 10:17".
//
// Covers the shapes the store actually holds (plan §2 measured four distinct
// expressions, three of them ``M H * * *``) and hands anything else back
// unchanged — a wrong plain-English schedule is worse than a raw one, and the
// detail panel prints the expression either way. Deliberately not a dependency:
// the whole grammar we need is four cases.
export function humanizeCron(expr: string | null | undefined, t: (k: string, opts?: any) => string): string {
  const raw = (expr ?? '').trim();
  if (!raw) return '';
  const fields = raw.split(/\s+/);
  if (fields.length !== 5) return raw;
  const [minute, hour, dayOfMonth, month, dayOfWeek] = fields;
  if (dayOfMonth !== '*' || month !== '*') return raw;

  if (hour === '*' && dayOfWeek === '*') {
    if (minute === '*') return t('harness.cron.everyMinute');
    const step = /^\*\/(\d+)$/.exec(minute);
    if (step && Number(step[1]) > 0) return t('harness.cron.everyMinutes', { count: Number(step[1]) });
    return raw;
  }

  const time = clockTime(hour, minute);
  if (!time) return raw;
  if (dayOfWeek === '*') return t('harness.cron.daily', { time });
  const days = weekdays(dayOfWeek);
  if (!days) return raw;
  if (days.length === 7) return t('harness.cron.daily', { time });
  return t('harness.cron.weekly', {
    days: days.map((day) => t(`harness.cron.weekday.${WEEKDAY_NAMES[day]}`)).join('、'),
    time,
  });
}

// ---------------------------------------------------------------------------
// Rows
// ---------------------------------------------------------------------------

export type HarnessDefinitionKind = 'task' | 'watch';

// The subset of a task/watch payload the row reads. Structural rather than the
// two concrete types so one set of helpers serves both lists, and so a test can
// state a case in four fields instead of thirty.
export type HarnessDefinitionFacts = {
  lifecycle_state?: HarnessLifecycleState | string | null;
  lifecycle_detail?: HarnessLifecycleDetail | string | null;
  next_run_at?: string | null;
  waiting_since?: string | null;
  process_alive?: boolean | null;
  mode?: string | null;
  schedule_type?: string | null;
  cron?: string | null;
  run_at?: string | null;
  last_event_at?: string | null;
  last_run_at?: string | null;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  updated_at?: string | null;
};

const WHITESPACE_RUN = /\s+/g;

// One uniform row title for a task or a watch (§4.3): its name, else the first
// non-empty line of the message it sends, else the kind. ``name`` wins here —
// unlike a run, a definition's name is the thing the user named it — and the
// message is what 40 of 54 live tasks have instead of a name, which is why they
// currently render as a hash.
//
// Never slices: the row truncates with CSS so the detail panel and the
// server-side search index keep the full text.
export function definitionRowTitle(
  row: { name?: string | null; message?: string | null; prompt?: string | null },
  kindLabel: string,
): string {
  const name = (row.name ?? '').trim().replace(WHITESPACE_RUN, ' ');
  if (name) return name;
  for (const line of (row.message || row.prompt || '').split('\n')) {
    const collapsed = line.trim().replace(WHITESPACE_RUN, ' ');
    if (collapsed) return collapsed;
  }
  return kindLabel;
}

// The last moment this row is known to have done anything.
//
// §4.2: never gate a whole block on one nullable field. Each candidate is the
// most precise answer for some row shape — a waiting watch has
// ``waiting_since``, a ``forever`` watch has ``last_event_at``, a task has
// ``last_run_at``, a retired row has ``last_finished_at`` — and ``updated_at``
// is non-null for every row, so the chain always lands. Mirrors the ordering
// coalesce ``list_watches_page`` already sorts by.
export function definitionActivityAt(row: HarnessDefinitionFacts): string | null {
  return (
    row.waiting_since ||
    row.last_event_at ||
    row.last_run_at ||
    row.last_finished_at ||
    row.updated_at ||
    null
  );
}

// Line 1's chip: what kind of schedule this is, in words. A watch says whether
// it fires once or keeps watching; a task says when it fires.
export function definitionChipLabel(
  row: HarnessDefinitionFacts,
  kind: HarnessDefinitionKind,
  t: (k: string, opts?: any) => string,
): string {
  if (kind === 'watch') {
    return t(row.mode === 'forever' ? 'harness.row.modeForever' : 'harness.row.modeOnce');
  }
  if (row.cron) return humanizeCron(row.cron, t);
  if (row.schedule_type === 'at' || row.run_at) return t('harness.row.modeOnce');
  return row.schedule_type || t('harness.unknownSchedule');
}

// What the row warns about, if anything: how a finished row ended, or a waiter
// whose process is gone while the row still claims to be armed. ``null`` when
// there is nothing to warn about — including when liveness is simply unknown,
// which must not be dressed up as "dead".
export type HarnessRowAlert = 'error' | 'timeout' | 'dead';

export type HarnessDefinitionLine = {
  primary: string;
  secondary: string | null;
  alert: HarnessRowAlert | null;
};

function livenessLabel(
  row: HarnessDefinitionFacts,
  t: (k: string) => string,
): string | null {
  if (row.process_alive === true) return t('harness.row.processAlive');
  if (row.process_alive === false) return t('harness.row.processDead');
  // ``null`` means we have never seen this waiter, which is not the same as
  // having seen it exit — say nothing rather than claim it is dead.
  return null;
}

// Line 2 of a task/watch row: state, not mechanism (§4.2).
//
// This is the line that used to print a wait's raw ``wait_pr.py`` argv, complete
// with a developer's absolute paths, or 790 characters of inline bash. None of
// that says whether the thing is still working; all of it is still one click
// away in the detail panel.
export function definitionRowLine(
  row: HarnessDefinitionFacts,
  kind: HarnessDefinitionKind,
  t: (k: string, opts?: any) => string,
  now: number = Date.now(),
): HarnessDefinitionLine {
  const state = row.lifecycle_state;
  const activityAt = definitionActivityAt(row);
  const liveness = kind === 'watch' ? livenessLabel(row, t) : null;
  const dead = kind === 'watch' && row.process_alive === false ? ('dead' as const) : null;

  if (state === 'finished') {
    const detail = row.lifecycle_detail;
    return {
      primary: lifecycleLabel('finished', detail, t),
      secondary: humanizeTime(row.last_finished_at || activityAt, t, now),
      alert: detail === 'error' || detail === 'timeout' ? detail : null,
    };
  }

  if (state === 'running') {
    const duration = humanizeGap(row.last_started_at || activityAt, now);
    return {
      primary: duration ? t('harness.row.runningFor', { duration }) : t('harness.lifecycle.running'),
      secondary: liveness,
      alert: dead,
    };
  }

  if (state === 'waiting') {
    if (kind === 'watch') {
      // A ``forever`` watch reports its most recent catch; a ``once`` watch has
      // nothing to report until it fires, so it reports how long it has been
      // waiting. Either way the fallback chain guarantees a time.
      const primary =
        row.mode === 'forever' && row.last_event_at
          ? t('harness.row.lastEvent', { when: humanizeTime(row.last_event_at, t, now) })
          : t('harness.row.waitingFor', { duration: humanizeGap(activityAt, now) ?? '—' });
      return { primary, secondary: liveness, alert: dead };
    }
    if (row.next_run_at) {
      const gap = humanizeGap(row.next_run_at, now);
      const when = humanizeTime(row.next_run_at, t, now);
      // A one-shot's headline is how long is left; a recurring task's is when
      // it next fires, with its previous fire as the secondary.
      if (row.schedule_type === 'at' || (!row.cron && row.run_at)) {
        return { primary: gap ? t('harness.row.nextIn', { duration: gap }) : when, secondary: when, alert: null };
      }
      return {
        primary: t('harness.row.nextAt', { when }),
        secondary: row.last_run_at ? t('harness.row.lastRun', { when: humanizeTime(row.last_run_at, t, now) }) : null,
        alert: null,
      };
    }
    return {
      primary: t('harness.row.waitingFor', { duration: humanizeGap(activityAt, now) ?? '—' }),
      secondary: row.last_run_at ? t('harness.row.lastRun', { when: humanizeTime(row.last_run_at, t, now) }) : null,
      alert: null,
    };
  }

  // ``paused``, and any state a future server sends that this client has no
  // word for — both render as "switched off, last seen <when>" rather than
  // blank.
  return {
    primary: lifecycleLabel(state ?? 'paused', null, t),
    secondary: humanizeTime(activityAt, t, now),
    alert: null,
  };
}
