import { describe, expect, it } from 'vitest';

import en from '../../i18n/en.json';
import zh from '../../i18n/zh.json';
import {
  DEFAULT_DEFINITION_STATUS,
  DEFINITION_STATUS_FILTERS,
  definitionActiveCount,
  definitionActivityAt,
  definitionChipLabel,
  definitionRowLine,
  definitionRowTitle,
  definitionStateSince,
  definitionStatusCount,
  definitionSurvivesToggle,
  formatWallTime,
  humanizeCron,
  humanizeGap,
  humanizeTime,
  isWallClockTimestamp,
  lifecycleLabel,
} from './harnessLifecycle';

// Translation-free stand-in: proves a mapper picked the right key without
// pinning the copy. Interpolations are appended so a test can assert the value
// reached the string.
//
// Duration units are the exception, resolved from the real bundle: "3h" is a
// value these mappers *compose*, not a key they choose, so leaving it as a
// dotted path would make every row assertion below unreadable and would assert
// nothing about what the reader sees.
const key = (k: string, opts?: Record<string, unknown>) => {
  if (k.startsWith('common.duration.')) return en.common.duration[k.replace('common.duration.', '')];
  return opts ? `${k}(${Object.values(opts).join(',')})` : k;
};

// The real bundles, so a key a mapper can emit but nobody translated fails
// here rather than shipping as its own dotted path. Both locales, because a
// key added to one side only is the same defect in the other language.
const BUNDLES: Record<string, unknown> = { en, zh };

// Asserts that every key these mappers can produce resolves to real copy, in
// every language. Reports the missing ones together — a run that names one
// key per re-run is what makes this class of fix take five rounds.
const expectCopy = (prefix: string, leaves: readonly string[]) => {
  const missing = Object.entries(BUNDLES).flatMap(([lng, bundle]) =>
    leaves
      .filter(
        (leaf) =>
          typeof `${prefix}.${leaf}`
            .split('.')
            .reduce<any>((node, part) => node?.[part], bundle) !== 'string',
      )
      .map((leaf) => `${lng}: ${prefix}.${leaf}`),
  );
  expect(missing).toEqual([]);
};

// A fixed "now" so every relative phrase is deterministic: 2026-07-26 13:00
// local, whatever timezone the runner is in.
const NOW = new Date(2026, 6, 26, 13, 0, 0).getTime();
const at = (offsetMs: number) => new Date(NOW + offsetMs).toISOString();
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe('lifecycleLabel', () => {
  it('resolves a finished row through how it ended, not the state', () => {
    expect(lifecycleLabel('finished', 'timeout', key)).toBe('harness.lifecycle.timeout');
    expect(lifecycleLabel('finished', 'error', key)).toBe('harness.lifecycle.error');
    expect(lifecycleLabel('finished', 'normal', key)).toBe('harness.lifecycle.normal');
  });

  it('treats a finished row with no recorded detail as a normal ending', () => {
    expect(lifecycleLabel('finished', null, key)).toBe('harness.lifecycle.normal');
  });

  it('names the live states directly, and never renders a bare state string', () => {
    expect(lifecycleLabel('waiting', null, key)).toBe('harness.lifecycle.waiting');
    expect(lifecycleLabel('running', null, key)).toBe('harness.lifecycle.running');
    expect(lifecycleLabel('paused', null, key)).toBe('harness.lifecycle.paused');
    // A state this client has no word for must not leak into the UI as itself.
    expect(lifecycleLabel('quiesced', null, key)).toBe('harness.lifecycle.unknown');
    expect(lifecycleLabel(null, null, key)).toBe('harness.lifecycle.unknown');
  });

  it('has real copy behind every key it can produce', () => {
    expectCopy('harness.lifecycle', [
      'running',
      'waiting',
      'paused',
      'finished',
      'normal',
      'timeout',
      'error',
      'unknown',
    ]);
  });
});

describe('definitionStatusCount', () => {
  const counts = { total: 1180, running: 2, waiting: 22, paused: 0, finished: 1156 };

  it('sums the states a chip selects', () => {
    expect(definitionStatusCount(counts, 'active')).toBe(24);
    expect(definitionStatusCount(counts, 'finished')).toBe(1156);
    expect(definitionStatusCount(counts, 'paused')).toBe(0);
  });

  it('offers the four owner-approved lifecycle chips', () => {
    // Exact ``waiting`` and ``running`` filters remain on the server for API and
    // CLI callers; the toolbar combines them under Active because each row
    // already names which one it is.
    expect([...DEFINITION_STATUS_FILTERS]).toEqual(['all', 'active', 'paused', 'finished']);
  });

  it('reads total for "all" rather than summing the states it knows', () => {
    // So a state added server-side still counts toward All here.
    expect(definitionStatusCount({ ...counts, quiesced: 7, total: 1187 }, 'all')).toBe(1187);
  });

  it('is the tab badge, by construction', () => {
    expect(definitionActiveCount(counts)).toBe(definitionStatusCount(counts, DEFAULT_DEFINITION_STATUS));
    // §4.4: the badge must never promise rows the landing view excludes.
    expect(definitionActiveCount(counts)).toBeLessThan(counts.total);
  });

  it('survives a server that sends nothing', () => {
    expect(definitionStatusCount(undefined, 'active')).toBe(0);
    expect(definitionStatusCount(null, 'all')).toBe(0);
  });

  it('has copy for every chip it offers', () => {
    expectCopy('harness.statusFilter', DEFINITION_STATUS_FILTERS);
  });
});

describe('definitionSurvivesToggle', () => {
  it('keeps a row under All whichever way the switch goes', () => {
    expect(definitionSurvivesToggle('all', true)).toBe(true);
    expect(definitionSurvivesToggle('all', false)).toBe(true);
  });

  it('drops a row the user just switched out of the current chip', () => {
    expect(definitionSurvivesToggle('active', false)).toBe(false);
    expect(definitionSurvivesToggle('paused', true)).toBe(false);
    expect(definitionSurvivesToggle('finished', true)).toBe(false);
  });

  it('keeps a row the switch moved into the current chip', () => {
    expect(definitionSurvivesToggle('active', true)).toBe(true);
    expect(definitionSurvivesToggle('paused', false)).toBe(true);
  });

  it('leaves a running row alone, because the switch does not stop it', () => {
    // An in-flight run outranks ``enabled`` in the lifecycle case, so switching
    // a running row off does not move it out of Active — it keeps running until
    // that run ends.
    expect(definitionSurvivesToggle('active', false, { lifecycle_state: 'running' })).toBe(true);
    expect(definitionSurvivesToggle('active', true, { lifecycle_state: 'running' })).toBe(true);
  });

  it('keeps a fired one-shot detail open when re-enabling cannot create a future fire', () => {
    const fired = {
      lifecycle_state: 'finished' as const,
      schedule_type: 'at',
      next_run_at: null,
    };
    expect(definitionSurvivesToggle('finished', true, fired)).toBe(true);
    expect(definitionSurvivesToggle('active', true, fired)).toBe(false);

    const retiredWatch = { lifecycle_state: 'finished' as const, next_run_at: null };
    expect(definitionSurvivesToggle('finished', true, retiredWatch)).toBe(false);
  });
});

describe('humanizeTime', () => {
  it('says today, tomorrow and yesterday with the clock time', () => {
    expect(humanizeTime(at(35 * MINUTE), key, NOW)).toBe('harness.when.today(13:35)');
    expect(humanizeTime(at(20 * HOUR), key, NOW)).toBe('harness.when.tomorrow(09:00)');
    expect(humanizeTime(at(-14 * HOUR), key, NOW)).toBe('harness.when.yesterday(23:00)');
  });

  it('counts calendar days, not 24-hour blocks', () => {
    // 23:50 today and 00:10 tomorrow are 20 minutes apart and two dates apart.
    const lateTonight = new Date(2026, 6, 26, 23, 50).getTime();
    const justAfterMidnight = new Date(2026, 6, 27, 0, 10).toISOString();
    expect(humanizeTime(justAfterMidnight, key, lateTonight)).toBe('harness.when.tomorrow(00:10)');
  });

  it('drops to a date once it is further out, and adds the year across one', () => {
    expect(humanizeTime(at(4 * DAY), key, NOW)).toBe('harness.when.dateTime(07-30,13:00)');
    expect(humanizeTime(new Date(2027, 0, 1, 0, 0).toISOString(), key, NOW)).toBe(
      'harness.when.dateTime(2027-01-01,00:00)',
    );
  });

  it('hands back garbage unchanged instead of printing "Invalid Date"', () => {
    expect(humanizeTime('not-a-time', key, NOW)).toBe('not-a-time');
    expect(humanizeTime(null, key, NOW)).toBe('—');
  });
});

describe('humanizeGap', () => {
  it('measures distance in either direction', () => {
    expect(humanizeGap(at(20 * MINUTE), NOW, key)).toBe('20m');
    expect(humanizeGap(at(-3 * HOUR), NOW, key)).toBe('3h');
  });

  it('reports nothing rather than zero when there is no timestamp', () => {
    expect(humanizeGap(null, NOW, key)).toBeNull();
    expect(humanizeGap('not-a-time', NOW, key)).toBeNull();
  });
});

describe('wall-clock timestamps', () => {
  it('tells an instant from a clock face', () => {
    expect(isWallClockTimestamp('2026-08-01T09:00:00Z')).toBe(false);
    expect(isWallClockTimestamp('2026-08-01T09:00:00+08:00')).toBe(false);
    expect(isWallClockTimestamp('2026-08-01T09:00:00+0800')).toBe(false);
    // What ``vibe task add --at "2026-08-01 09:00"`` stores: a reading on some
    // other zone's clock, which ``new Date`` would resolve against the viewer's.
    expect(isWallClockTimestamp('2026-08-01T09:00:00')).toBe(true);
    expect(isWallClockTimestamp('2026-08-01 09:00')).toBe(true);
    expect(isWallClockTimestamp(null)).toBe(false);
  });

  it('prints the clock face with the zone it belongs to, and converts nothing', () => {
    expect(formatWallTime('2026-08-01T09:00:00', 'Asia/Tokyo', key)).toBe(
      'harness.when.wallClock(2026-08-01 09:00,Asia/Tokyo)',
    );
    // No zone to name: still the clock as written, never shifted.
    expect(formatWallTime('2026-08-01T09:00:00', null, key)).toBe('2026-08-01 09:00');
    // Not timestamp-shaped: verbatim rather than reformatted into a guess.
    expect(formatWallTime('whenever', null, key)).toBe('whenever');
    expect(formatWallTime(null, 'Asia/Tokyo', key)).toBe('—');
  });

  it('has copy for the zone-qualified form', () => {
    expectCopy('harness.when', ['wallClock']);
  });
});

describe('humanizeCron', () => {
  it('speaks the shapes the store actually holds', () => {
    expect(humanizeCron('17 10 * * *', key)).toBe('harness.cron.daily(10:17)');
    expect(humanizeCron('*/5 * * * *', key)).toBe('harness.cron.everyMinutes(5)');
    expect(humanizeCron('* * * * *', key)).toBe('harness.cron.everyMinute');
  });

  // The days a weekly phrase names, pulled back out of the stand-in's output.
  // Splits on the *key* the separator now resolves to, which is what the
  // stand-in emits in place of the copy — the hardcoded "、" this used to split
  // on was the bug: it shipped a Chinese separator into English rows.
  const SEPARATOR = 'harness.cron.weekdaySeparator';
  const cronDays = (expr: string) =>
    /weekly\((.*),\d\d:\d\d\)$/
      .exec(humanizeCron(expr, key))?.[1]
      .split(SEPARATOR)
      .map((day) => day.replace('harness.cron.weekday.', '')) ?? null;

  // APScheduler's numbering, which ``CronTrigger.from_crontab`` imposes on every
  // expression this app stores: Monday is 0, Sunday is 6. NOT crontab(5)'s. The
  // Python mirror test pins this table against the library itself.
  it('numbers weekdays the way the scheduler that fires them does', () => {
    expect(cronDays('0 8 * * 0')).toEqual(['mon']);
    expect(cronDays('0 8 * * 6')).toEqual(['sun']);
    expect(cronDays('0 8 * * SUN')).toEqual(['sun']);
    expect(cronDays('0 8 * * mon,wed,fri')).toEqual(['mon', 'wed', 'fri']);
    // The case that matters most: someone writing crontab(5)'s "weekdays" gets
    // Tue–Sat out of APScheduler. Saying "Mon–Fri" would hide a real surprise
    // behind a familiar phrase; the row shows what will actually happen.
    expect(cronDays('0 9 * * 1-5')).toEqual(['tue', 'wed', 'thu', 'fri', 'sat']);
    expect(cronDays('0 9 * * 0-4')).toEqual(['mon', 'tue', 'wed', 'thu', 'fri']);
  });

  it('lists days once, in week order, however they were written', () => {
    expect(cronDays('0 8 * * 4,0,0,SAT')).toEqual(['mon', 'fri', 'sat']);
  });

  it('collapses an every-weekday expression back to daily', () => {
    expect(humanizeCron('0 8 * * 0-6', key)).toBe('harness.cron.daily(08:00)');
  });

  it('hands back anything it cannot say plainly, rather than guessing', () => {
    // A wrong plain-English schedule is worse than a raw one; the detail panel
    // prints the expression either way.
    for (const expr of [
      '0 0 1 * *', // day-of-month
      '0 0 * 3 *', // month
      '0 9-17 * * *', // hour range
      '0 0 * * 1#2', // nth weekday
      '@daily', // non-standard
      '1 2 3 4', // too few fields
      '0 99 * * *', // out of range
    ]) {
      expect(humanizeCron(expr, key)).toBe(expr);
    }
  });

  it('refuses the day-of-week forms APScheduler will not schedule', () => {
    // Both are legal crontab(5) and both raise in ``CronTrigger.from_crontab``:
    // ``7`` exceeds the maximum of 6, and a descending range is rejected rather
    // than wrapped. A task carrying either never fires, so describing it as a
    // weekly schedule would put a promise on screen that nothing can keep.
    expect(humanizeCron('0 8 * * 7', key)).toBe('0 8 * * 7');
    expect(humanizeCron('0 8 * * 5-1', key)).toBe('0 8 * * 5-1');
  });

  it('has copy behind every phrase and weekday it can produce', () => {
    expectCopy('harness.cron', [
      'everyMinute',
      'everyMinutes',
      'daily',
      'weekly',
      'weekdaySeparator',
    ]);
    expectCopy('harness.cron.weekday', ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']);
  });

  it('separates weekdays with copy, not a hardcoded Chinese comma', () => {
    // en and zh punctuate lists differently, and the separator was baked into
    // the mapper — "Mon、Wed、Fri" reached English rows.
    expect(en.harness.cron.weekdaySeparator).not.toBe(zh.harness.cron.weekdaySeparator);
  });
});

describe('definitionRowTitle', () => {
  it('prefers the name the user gave it', () => {
    expect(definitionRowTitle({ name: 'Nightly digest', message: 'ignored' }, 'Task')).toBe('Nightly digest');
  });

  it('falls back to the first real line of the message it sends', () => {
    // 40 of 54 live tasks have no name; without this they render as a hash.
    expect(definitionRowTitle({ name: null, message: '\n\n  Summarize #ops\nand post it  ' }, 'Task')).toBe(
      'Summarize #ops',
    );
  });

  it('collapses whitespace so a wrapped line stays one line', () => {
    expect(definitionRowTitle({ name: '  Weekly\t\tdigest ' }, 'Task')).toBe('Weekly digest');
  });

  it('never slices — truncation is the row\'s CSS job, not the title\'s', () => {
    const long = 'x'.repeat(400);
    expect(definitionRowTitle({ name: long }, 'Task')).toBe(long);
  });

  it('names the kind when there is nothing else to say', () => {
    expect(definitionRowTitle({ name: null, message: '   \n\t' }, 'Watch')).toBe('Watch');
  });
});

describe('definitionActivityAt', () => {
  it('walks the fallback chain instead of gating on one nullable field', () => {
    expect(definitionActivityAt({ waiting_since: 'a', last_event_at: 'b', updated_at: 'z' })).toBe('a');
    expect(definitionActivityAt({ last_event_at: 'b', last_run_at: 'c', updated_at: 'z' })).toBe('b');
    expect(definitionActivityAt({ last_run_at: 'c', updated_at: 'z' })).toBe('c');
    expect(definitionActivityAt({ last_finished_at: 'd', updated_at: 'z' })).toBe('d');
    // updated_at is non-null for every row, so the chain always lands.
    expect(definitionActivityAt({ updated_at: 'z' })).toBe('z');
  });
});

describe('definitionChipLabel', () => {
  it('says whether a watch fires once or keeps watching', () => {
    expect(definitionChipLabel({ mode: 'forever' }, 'watch', key)).toBe('harness.row.modeForever');
    expect(definitionChipLabel({ mode: 'once' }, 'watch', key)).toBe('harness.row.modeOnce');
    expect(definitionChipLabel({}, 'watch', key)).toBe('harness.row.modeOnce');
  });

  it('says when a task fires, in words', () => {
    expect(definitionChipLabel({ cron: '17 10 * * *' }, 'task', key)).toBe('harness.cron.daily(10:17)');
    expect(definitionChipLabel({ schedule_type: 'at', run_at: at(HOUR) }, 'task', key)).toBe('harness.row.modeOnce');
  });
});

describe('definitionRowLine', () => {
  it('tells a dead waiter from a healthy one', () => {
    const dead = definitionRowLine(
      { lifecycle_state: 'waiting', mode: 'once', waiting_since: at(-3 * HOUR), process_alive: false },
      'watch',
      key,
      NOW,
    );
    expect(dead.primary).toBe('harness.row.waitingFor(3h)');
    expect(dead.secondary).toBe('harness.row.processDead');
    expect(dead.alert).toBe('dead');

    const alive = definitionRowLine(
      { lifecycle_state: 'waiting', mode: 'once', waiting_since: at(-3 * HOUR), process_alive: true },
      'watch',
      key,
      NOW,
    );
    expect(alive.secondary).toBe('harness.row.processAlive');
    expect(alive.alert).toBeNull();
  });

  it('never claims a waiter is dead just because nobody looked', () => {
    const unknown = definitionRowLine(
      { lifecycle_state: 'waiting', mode: 'once', waiting_since: at(-HOUR), process_alive: null },
      'watch',
      key,
      NOW,
    );
    expect(unknown.secondary).toBeNull();
    expect(unknown.alert).toBeNull();
  });

  it('still prints a time for a forever watch that has never caught anything', () => {
    // The defect this replaces: the whole block was gated on last_event_at, so
    // a watch that had waited three days showed no time at all.
    const line = definitionRowLine(
      { lifecycle_state: 'waiting', mode: 'forever', last_event_at: null, waiting_since: at(-3 * DAY) },
      'watch',
      key,
      NOW,
    );
    expect(line.primary).toBe('harness.row.waitingFor(3d)');
  });

  it('leads a forever watch with its most recent catch once it has one', () => {
    const line = definitionRowLine(
      { lifecycle_state: 'waiting', mode: 'forever', last_event_at: at(-2 * HOUR), waiting_since: at(-3 * DAY) },
      'watch',
      key,
      NOW,
    );
    expect(line.primary).toBe('harness.row.lastEvent(harness.when.today(11:00))');
  });

  it('says how a finished row ended, and when', () => {
    const timedOut = definitionRowLine(
      { lifecycle_state: 'finished', lifecycle_detail: 'timeout', last_finished_at: at(-DAY) },
      'watch',
      key,
      NOW,
    );
    expect(timedOut.primary).toBe('harness.lifecycle.timeout');
    expect(timedOut.secondary).toBe('harness.when.yesterday(13:00)');
    expect(timedOut.alert).toBe('timeout');

    const ok = definitionRowLine(
      { lifecycle_state: 'finished', lifecycle_detail: 'normal', last_finished_at: at(-DAY) },
      'watch',
      key,
      NOW,
    );
    expect(ok.alert).toBeNull();
  });

  it('counts down a one-shot task and names its next fire for a cron one', () => {
    const oneShot = definitionRowLine(
      { lifecycle_state: 'waiting', schedule_type: 'at', run_at: at(20 * MINUTE), next_run_at: at(20 * MINUTE) },
      'task',
      key,
      NOW,
    );
    expect(oneShot.primary).toBe('harness.row.nextIn(20m)');
    expect(oneShot.secondary).toBe('harness.when.today(13:20)');

    const recurring = definitionRowLine(
      {
        lifecycle_state: 'waiting',
        cron: '17 10 * * *',
        next_run_at: at(21 * HOUR + 17 * MINUTE),
        last_run_at: at(-2 * HOUR - 43 * MINUTE),
      },
      'task',
      key,
      NOW,
    );
    expect(recurring.primary).toBe('harness.row.nextAt(harness.when.tomorrow(10:17))');
    expect(recurring.secondary).toBe('harness.row.lastRun(harness.when.today(10:17))');
  });

  it('does not count upward after a one-shot deadline passes', () => {
    const line = definitionRowLine(
      { lifecycle_state: 'waiting', schedule_type: 'at', run_at: at(-MINUTE), next_run_at: at(-MINUTE) },
      'task',
      key,
      NOW,
    );

    expect(line.primary).toBe('harness.when.today(12:59)');
    expect(line.primary).not.toContain('harness.row.nextIn');
  });

  it('still says something for a waiting task the scheduler has not dated yet', () => {
    const line = definitionRowLine(
      { lifecycle_state: 'waiting', cron: '17 10 * * *', next_run_at: null, updated_at: at(-45 * MINUTE) },
      'task',
      key,
      NOW,
    );
    expect(line.primary).toBe('harness.row.waitingFor(45m)');
  });

  it('reports how long a running row has been running', () => {
    const line = definitionRowLine(
      { lifecycle_state: 'running', running_since: at(-12 * MINUTE), process_alive: true },
      'watch',
      key,
      NOW,
    );
    expect(line.primary).toBe('harness.row.runningFor(12m)');
    expect(line.secondary).toBe('harness.row.processAlive');
  });

  it('times a running row from the run that is running, not from its last cycle', () => {
    // A forever watch that caught something yesterday and started a fresh run a
    // minute ago. The activity chain would answer "1d" — a duration nothing is
    // spending, assembled from two different cycles.
    const line = definitionRowLine(
      {
        lifecycle_state: 'running',
        running_since: at(-MINUTE),
        last_event_at: at(-DAY),
        last_started_at: at(-DAY),
        waiting_since: at(-DAY),
        mode: 'forever',
      },
      'watch',
      key,
      NOW,
    );
    expect(line.primary).toBe('harness.row.runningFor(1m)');
  });

  it('prints no duration for a run that is queued rather than started', () => {
    // ``running`` covers queued-or-running, and a queued run has no start. The
    // state alone is the whole truth; inventing "running 0s" or reaching back to
    // the previous cycle would not be.
    const line = definitionRowLine(
      { lifecycle_state: 'running', running_since: null, last_started_at: at(-DAY) },
      'task',
      key,
      NOW,
    );
    expect(line.primary).toBe('harness.lifecycle.running');
  });

  it('falls back to a state and a time for paused rows and for states it has no word for', () => {
    const paused = definitionRowLine({ lifecycle_state: 'paused', updated_at: at(-DAY) }, 'task', key, NOW);
    expect(paused.primary).toBe('harness.lifecycle.paused');
    expect(paused.secondary).toBe('harness.when.yesterday(13:00)');

    const unknown = definitionRowLine({ lifecycle_state: 'quiesced', updated_at: at(-DAY) }, 'task', key, NOW);
    expect(unknown.primary).toBe('harness.lifecycle.unknown');
    expect(unknown.secondary).toBe('harness.when.yesterday(13:00)');
  });

  it('prints a time in every branch — no row is ever left without one', () => {
    // ``running`` carries ``running_since`` here rather than leaning on
    // ``updated_at`` like the others: it is the one branch with a real source
    // for its duration, and reaching past it to the activity chain is the very
    // thing that let a row claim a duration from a previous cycle. A running row
    // with no start is covered above — it prints the state and no time, on
    // purpose.
    const cases: Array<[string, Parameters<typeof definitionRowLine>[0], 'task' | 'watch']> = [
      ['waiting once watch', { lifecycle_state: 'waiting', mode: 'once', updated_at: at(-HOUR) }, 'watch'],
      ['waiting forever watch', { lifecycle_state: 'waiting', mode: 'forever', updated_at: at(-HOUR) }, 'watch'],
      ['finished watch', { lifecycle_state: 'finished', updated_at: at(-HOUR) }, 'watch'],
      ['running watch', { lifecycle_state: 'running', running_since: at(-HOUR) }, 'watch'],
      ['waiting task', { lifecycle_state: 'waiting', updated_at: at(-HOUR) }, 'task'],
      ['paused task', { lifecycle_state: 'paused', updated_at: at(-HOUR) }, 'task'],
      ['stateless row', { updated_at: at(-HOUR) }, 'task'],
    ];
    for (const [name, row, kind] of cases) {
      const line = definitionRowLine(row, kind, key, NOW);
      expect(`${name}: ${line.primary} ${line.secondary ?? ''}`).toMatch(/harness\.(when|row)\./);
    }
  });

  it('dates a pause by the toggle, not by what the row was doing before it', () => {
    // A cron task paused a minute ago that last ran yesterday. The activity
    // chain answers "when did this row last do anything" and would say
    // yesterday — which reads as "paused since yesterday", a wait the user
    // never had. ``set_definition_enabled`` stamps ``updated_at`` when it
    // flips, so the toggle time is on the row and is the only fact that
    // describes the pause.
    const row = {
      lifecycle_state: 'paused',
      cron: '0 * * * *',
      last_run_at: at(-DAY),
      last_event_at: at(-DAY),
      updated_at: at(-MINUTE),
    };
    expect(definitionStateSince(row, 'paused')).toBe(row.updated_at);
    expect(definitionRowLine(row, 'task', key, NOW).secondary).toBe('harness.when.today(12:59)');
  });

  it('gives every state its own timestamp instead of one shared chain', () => {
    // The two defects this table replaced were the same defect twice: first
    // ``running`` read the chain and dated a fresh run to the previous cycle,
    // then ``paused`` read it and dated a pause to yesterday's run. Stating the
    // fact per state is what stops the third one.
    const row = {
      running_since: at(-MINUTE),
      waiting_since: at(-HOUR),
      last_finished_at: at(-DAY),
      updated_at: at(-2 * MINUTE),
      last_run_at: at(-3 * DAY),
    };
    expect(definitionStateSince(row, 'running')).toBe(row.running_since);
    expect(definitionStateSince(row, 'waiting')).toBe(row.waiting_since);
    expect(definitionStateSince(row, 'finished')).toBe(row.last_finished_at);
    expect(definitionStateSince(row, 'paused')).toBe(row.updated_at);
    // A queued run has not started, so ``running`` has no duration to offer and
    // must not borrow one.
    expect(definitionStateSince({ last_run_at: at(-3 * DAY) }, 'running')).toBeNull();
  });

  it('does not report an intentionally retired waiter as dead', () => {
    // A one-shot watch that just fired: the same call that recorded the catch
    // stopped its waiter, and the agent run it spawned is still queued, so the
    // row is ``running`` with a stopped process. That is the success path —
    // warning here reports a working watch as a monitoring failure.
    const retired = definitionRowLine(
      { lifecycle_state: 'running', mode: 'once', enabled: false, process_alive: false, running_since: at(-MINUTE) },
      'watch',
      key,
      NOW,
    );
    expect(retired.alert).toBeNull();
    expect(retired.secondary).toBeNull();

    // Still armed and the process is gone: that contradiction is the alert.
    const armed = definitionRowLine(
      { lifecycle_state: 'waiting', mode: 'forever', enabled: true, process_alive: false, waiting_since: at(-HOUR) },
      'watch',
      key,
      NOW,
    );
    expect(armed.alert).toBe('dead');
    expect(armed.secondary).toBe('harness.row.processDead');

    // A payload that does not state the switch is not evidence of a shutdown.
    const unstated = definitionRowLine(
      { lifecycle_state: 'waiting', mode: 'once', process_alive: false, waiting_since: at(-HOUR) },
      'watch',
      key,
      NOW,
    );
    expect(unstated.alert).toBe('dead');
  });

  it('has copy behind every row phrase it can produce', () => {
    expectCopy('harness.row', [
      'modeOnce',
      'modeForever',
      'processAlive',
      'processDead',
      'runningFor',
      'waitingFor',
      'lastEvent',
      'nextIn',
      'nextAt',
      'lastRun',
    ]);
    expectCopy('harness.when', ['today', 'tomorrow', 'yesterday', 'dateTime']);
    // Every duration these rows print goes through the same four leaves, so a
    // unit added in English only fails here instead of shipping "3d" to a
    // Chinese reader.
    expectCopy('common.duration', ['seconds', 'minutes', 'hours', 'days']);
  });
});
