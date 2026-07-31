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
export function definitionSurvivesToggle(
  status: string,
  enabled: boolean,
  row?: HarnessDefinitionFacts | null,
): boolean {
  const states = DEFINITION_STATUS_FILTER_STATES[status];
  if (!states || states.length === 0) return true;
  const state = row?.lifecycle_state;
  // An in-flight run outranks ``enabled`` in the lifecycle case, so flipping
  // the switch on a running row leaves it exactly where it is until that run
  // ends. Without this the ``running`` chip would close the panel for a row
  // that never moved.
  if (state === 'running') return states.includes('running');
  // A finished one-shot has no future fire, and the switch cannot invent one.
  // Ask the same question as the server from the resolved schedule fact instead
  // of treating ``enabled`` as a promise: re-enabling this row leaves it under
  // Finished, so its open detail panel stays there too.
  if (state === 'finished' && row?.schedule_type === 'at' && !row.next_run_at) {
    return states.includes('finished');
  }
  // Otherwise the switch *is* the state: on makes the row ``waiting``; off
  // takes it out of every live chip, into ``paused`` or ``finished`` — which of
  // the two is the server's call, and the refresh that follows settles it.
  if (enabled) return states.includes('waiting');
  return !states.some((live) => LIVE_STATES.includes(live));
}

// A finished row names its outcome only when the store provides one. Missing or
// unrecognised detail still proves the lifecycle state, but not how it ended.
export function lifecycleLabel(
  state: string | null | undefined,
  detail: string | null | undefined,
  t: (k: string) => string,
): string {
  if (state === 'finished') return t(`harness.lifecycle.${detail && DETAILS.has(detail) ? detail : 'finished'}`);
  if (state && STATES.has(state)) return t(`harness.lifecycle.${state}`);
  return t('harness.lifecycle.unknown');
}

const STATES = new Set<string>(['running', 'waiting', 'paused', 'finished']);
export const LIFECYCLE_DETAILS = ['normal', 'timeout', 'error'] as const;
const DETAILS = new Set<string>(LIFECYCLE_DETAILS);

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

// Compact magnitude between an instant and now, in R1's duration vocabulary
// ("42s" / "7m" / "3h"). Callers using direction-bearing copy must compare
// the instant with ``now`` before naming it. The unit words come from the locale,
// so ``t`` travels with the number rather than being bolted on later.
export function humanizeGap(
  iso: string | null | undefined,
  now: number,
  t: (key: string) => string,
): string | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  return formatElapsed(Math.abs(at - now) / 1000, t);
}

// A timestamp with no UTC offset is not an instant — it is a wall-clock reading
// in some zone the string does not name. ``new Date(...)`` resolves it against
// the *browser's* zone, so a one-shot stored as "09:00 in Asia/Tokyo" renders as
// 09:00 to a viewer in Los Angeles, who is 16 hours out. The scheduler resolves
// these against the row's own ``timezone``; the browser must not guess.
const UTC_OFFSET_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/i;

export function isWallClockTimestamp(iso: string | null | undefined): boolean {
  return !!iso && !UTC_OFFSET_SUFFIX.test(iso.trim());
}

// Print a wall-clock reading as what it is: the clock face, plus the zone it
// belongs to. No "today/tomorrow", because which day it falls on depends on the
// zone, and no conversion, because converting is the bug.
export function formatWallTime(
  iso: string | null | undefined,
  timezone: string | null | undefined,
  t: (k: string, opts?: any) => string,
): string {
  if (!iso) return '—';
  const raw = iso.trim();
  // Date and hh:mm only; seconds are noise on a schedule. Anything that is not
  // shaped like a timestamp is printed verbatim rather than reformatted.
  const parts = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/.exec(raw);
  const clock = parts ? `${parts[1]} ${parts[2]}` : raw;
  return timezone ? t('harness.when.wallClock', { time: clock, timezone }) : clock;
}

// ---------------------------------------------------------------------------
// Cron, in words
// ---------------------------------------------------------------------------

// APScheduler's week numbering, which is NOT crontab(5)'s.
//
// ``CronTrigger.from_crontab`` — what ``compute_next_run_at`` and the scheduler
// itself use — hands the day-of-week field straight to APScheduler's own parser,
// where Monday is 0 and Sunday is 6. Under crontab(5) Sunday is 0. Describing
// ``0 9 * * 1-5`` as "Mon–Fri" while the task actually fires Tue–Sat is exactly
// the class of lie this surface exists to remove, so the words follow the
// scheduler rather than the convention the expression was probably written in.
//
// Pinned to APScheduler's own ``WEEKDAYS`` constant by a test in
// ``tests/test_harness_definition_lifecycle.py`` — upgrade the library's
// numbering and that test fails rather than the rows quietly shifting a day.
const WEEKDAY_NAMES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const WEEKDAY_INDEX: Record<string, number> = WEEKDAY_NAMES.reduce(
  (map, name, index) => ({ ...map, [name]: index }),
  {},
);

function weekdayNumber(token: string): number | null {
  const named = WEEKDAY_INDEX[token.toLowerCase()];
  if (named != null) return named;
  if (!/^\d+$/.test(token)) return null;
  const value = Number(token);
  // No ``7``: APScheduler raises on it ("higher than the maximum value (6)"),
  // so an expression containing it never fires and must not be described as a
  // schedule that does.
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
    // No wrap-around: crontab(5) accepts FRI-MON, APScheduler rejects it ("the
    // minimum value in a range must not be higher than the maximum"). A range
    // it refuses to schedule gets described as the raw expression it is.
    if (start > end) return null;
    for (let day = start; day <= end; day += 1) days.add(day);
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
    days: days
      .map((day) => t(`harness.cron.weekday.${WEEKDAY_NAMES[day]}`))
      .join(t('harness.cron.weekdaySeparator')),
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
  running_since?: string | null;
  process_alive?: boolean | null;
  enabled?: boolean | null;
  timezone?: string | null;
  mode?: string | null;
  schedule_type?: string | null;
  cron?: string | null;
  run_at?: string | null;
  last_event_at?: string | null;
  last_run_at?: string | null;
  last_started_at?: string | null;
  last_finished_at?: string | null;
  last_error?: string | null;
  updated_at?: string | null;
  // Derived server-side from the definition's own run history, because
  // ``last_run_at``/``last_error`` are overwritten on every fire and one success
  // therefore erased days of failure.
  health?: HarnessDefinitionHealth | string | null;
  consecutive_failures?: number | null;
  recent_failures?: number | null;
};

// ``failing`` = the newest verdict failed; ``degraded`` = the newest succeeded but
// a failure is still in the window; ``unknown`` = health could not be computed,
// which is deliberately not the same as ``healthy``.
export const HARNESS_HEALTH_STATES = ['failing', 'degraded', 'healthy', 'unknown'] as const;
export type HarnessDefinitionHealth = (typeof HARNESS_HEALTH_STATES)[number];
const HEALTH_STATES = new Set<string>(HARNESS_HEALTH_STATES);

export function definitionHealth(row: HarnessDefinitionFacts): HarnessDefinitionHealth | null {
  const value = row.health;
  return value && HEALTH_STATES.has(value) ? (value as HarnessDefinitionHealth) : null;
}

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

// The moment each state is measured *from*.
//
// The activity chain above answers one question — "when did this row last do
// anything". States with a more precise stored fact use it; paused has no
// transition timestamp in the store, so it deliberately returns no time.
export function definitionStateSince(
  row: HarnessDefinitionFacts,
  state: HarnessLifecycleState | string | null | undefined,
): string | null {
  switch (state) {
    // Derived from the run that *is* running. Null while that run is queued —
    // it has not started, so there is no duration, and the state alone is the
    // honest answer.
    case 'running':
      return row.running_since ?? null;
    // ``waiting_since`` is the server's answer and heads the chain; the chain's
    // remaining candidates cover a row that has never started.
    case 'waiting':
      return row.waiting_since ?? definitionActivityAt(row);
    // ``updated_at`` also moves on edits while disabled, so no persisted field
    // can prove when the pause began.
    case 'paused':
      return null;
    // Prefer the most specific recorded event. A one-shot deadline is only the
    // fallback when neither a stored finish nor an actual run exists.
    case 'finished':
      return (
        row.last_finished_at ??
        row.last_run_at ??
        (row.schedule_type === 'at' ? (row.run_at ?? null) : null) ??
        definitionActivityAt(row)
      );
    default:
      return definitionActivityAt(row);
  }
}

// Whether this row still expects its waiter process to be up.
//
// A stopped waiter is only news while the switch says "keep watching". A
// one-shot watch that fired has its waiter stopped on purpose — by the same
// call that switched it off — and may well still be ``running`` because the
// agent run it spawned is queued. Warning there reports a successful catch as
// a monitoring failure. ``undefined`` means the payload did not say, and an
// unstated switch is not evidence of an intentional shutdown.
export function waiterExpectedAlive(row: HarnessDefinitionFacts): boolean {
  return row.enabled !== false;
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

// What the row warns about, if anything: how a finished row ended, a waiter
// whose process is gone while the row still claims to be armed, or a health
// verdict the server could not compute. ``null`` is reserved for rows with
// nothing to report — which includes unknown *liveness* (never dressed up as
// "dead") but not unknown *health*, because a health the projection failed to
// read is the one thing that must not render as a passing row.
export type HarnessRowAlert = 'error' | 'timeout' | 'dead' | 'degraded' | 'unknown';

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
  // Report an exit only where an exit is unexpected. A retired waiter did stop,
  // but saying so reads as a fault report on a row that did its job.
  if (row.process_alive === false && waiterExpectedAlive(row)) return t('harness.row.processDead');
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
  // Every branch below reads this, never the activity chain directly: the state
  // decides which timestamp describes it. See ``definitionStateSince``.
  const since = definitionStateSince(row, state);
  const liveness = kind === 'watch' ? livenessLabel(row, t) : null;
  const dead =
    kind === 'watch' && row.process_alive === false && waiterExpectedAlive(row) ? ('dead' as const) : null;
  // A recurring definition that fails every night is ``waiting`` between fires,
  // and every non-finished branch below used to return ``alert: null`` — so the
  // one state a broken cron actually sits in was the one state that could not warn.
  // ``finished`` keeps reporting how it ended; the health alert is what the other
  // states gain.
  // ``healthy`` is the only verdict that stays silent. ``unknown`` gets its own
  // alert rather than falling through to ``null``: the server emits it when the
  // health read itself failed, and collapsing that into "nothing to report"
  // hands the operator a clean list exactly when the failure signal is missing.
  const health = definitionHealth(row);
  const unhealthy: HarnessRowAlert | null =
    health === 'failing'
      ? 'error'
      : health === 'degraded'
        ? 'degraded'
        : health === 'unknown'
          ? 'unknown'
          : null;

  if (state === 'finished') {
    const detail = row.lifecycle_detail;
    return {
      primary: lifecycleLabel('finished', detail, t),
      secondary: humanizeTime(since, t, now),
      alert: detail === 'error' || detail === 'timeout' ? detail : unhealthy,
    };
  }

  if (state === 'running') {
    const duration = humanizeGap(since, now, t);
    return {
      primary: duration ? t('harness.row.runningFor', { duration }) : t('harness.lifecycle.running'),
      secondary: liveness,
      alert: dead ?? unhealthy,
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
          : t('harness.row.waitingFor', { duration: humanizeGap(since, now, t) ?? '—' });
      return { primary, secondary: liveness, alert: dead ?? unhealthy };
    }
    if (row.next_run_at) {
      const gap = humanizeGap(row.next_run_at, now, t);
      const when = humanizeTime(row.next_run_at, t, now);
      const nextRunMs = Date.parse(row.next_run_at);
      const isFuture = Number.isFinite(nextRunMs) && nextRunMs > now;
      // A one-shot's headline is how long is left; a recurring task's is when
      // it next fires, with its previous fire as the secondary.
      if (row.schedule_type === 'at' || (!row.cron && row.run_at)) {
        return {
          primary: gap && isFuture ? t('harness.row.nextIn', { duration: gap }) : when,
          secondary: when,
          alert: unhealthy,
        };
      }
      return {
        primary: t('harness.row.nextAt', { when }),
        secondary: row.last_run_at ? t('harness.row.lastRun', { when: humanizeTime(row.last_run_at, t, now) }) : null,
        alert: unhealthy,
      };
    }
    return {
      primary: t('harness.row.waitingFor', { duration: humanizeGap(since, now, t) ?? '—' }),
      secondary: row.last_run_at ? t('harness.row.lastRun', { when: humanizeTime(row.last_run_at, t, now) }) : null,
      alert: unhealthy,
    };
  }

  // Paused has no transition timestamp in today's schema. Unknown future states
  // still keep the activity fallback so a newer server does not lose context.
  return {
    primary: lifecycleLabel(state ?? 'paused', null, t),
    secondary: since ? humanizeTime(since, t, now) : null,
    alert: unhealthy,
  };
}
