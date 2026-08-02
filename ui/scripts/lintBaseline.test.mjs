import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { compareToBaseline, fatalProblems, missingRationales, pairKey, reportLines } from './lint-baseline.mjs';

// The ratchet's whole value is in this comparison: it decides what counts as new
// debt. Every branch is pinned here so the gate can't be widened by accident —
// a bug that turns "unclassified" into "allowed" would silently reopen the door
// the baseline exists to close.

describe('compareToBaseline accepts the recorded debt unchanged', () => {
  it('reports nothing when the counts match exactly', () => {
    const ledger = { 'src/a.ts': { 'no-explicit-any': 3 }, 'src/b.ts': { 'set-state-in-effect': 1 } };
    expect(compareToBaseline(ledger, structuredClone(ledger))).toEqual({
      unclassified: [],
      expanded: [],
      stale: [],
    });
  });
});

describe('compareToBaseline fails on new debt', () => {
  it('flags a rule violated in a file the baseline never listed', () => {
    const { unclassified } = compareToBaseline({}, { 'src/new.ts': { 'no-explicit-any': 1 } });
    expect(unclassified).toEqual([{ file: 'src/new.ts', rule: 'no-explicit-any', count: 1 }]);
  });

  it('flags a rule new to a file that already carries other baselined debt', () => {
    // The lookup has to be per (file, rule). A file-level check would let every
    // other rule in an already-listed file through unnoticed.
    const { unclassified, expanded } = compareToBaseline(
      { 'src/a.ts': { 'no-explicit-any': 3 } },
      { 'src/a.ts': { 'no-explicit-any': 3, 'set-state-in-effect': 2 } },
    );
    expect(unclassified).toEqual([{ file: 'src/a.ts', rule: 'set-state-in-effect', count: 2 }]);
    expect(expanded).toEqual([]);
  });

  it('flags a baselined rule violated more often than recorded', () => {
    const { expanded, unclassified } = compareToBaseline(
      { 'src/a.ts': { 'no-explicit-any': 3 } },
      { 'src/a.ts': { 'no-explicit-any': 4 } },
    );
    expect(expanded).toEqual([{ file: 'src/a.ts', rule: 'no-explicit-any', count: 4, allowed: 3 }]);
    expect(unclassified).toEqual([]);
  });
});

describe('compareToBaseline fails on a baseline that overstates the debt', () => {
  // Improvements have to be recorded, otherwise the freed-up headroom silently
  // becomes budget for new violations and the ratchet stops ratcheting.
  it('flags a rule now violated less often than recorded', () => {
    const { stale } = compareToBaseline(
      { 'src/a.ts': { 'no-explicit-any': 3 } },
      { 'src/a.ts': { 'no-explicit-any': 1 } },
    );
    expect(stale).toEqual([{ file: 'src/a.ts', rule: 'no-explicit-any', count: 1, allowed: 3 }]);
  });

  it('flags a rule that is now clean', () => {
    const { stale } = compareToBaseline({ 'src/a.ts': { 'no-explicit-any': 3 } }, {});
    expect(stale).toEqual([{ file: 'src/a.ts', rule: 'no-explicit-any', count: 0, allowed: 3 }]);
  });

  it('flags every rule of a file that no longer exists', () => {
    const { stale } = compareToBaseline(
      { 'src/gone.ts': { 'no-explicit-any': 2, 'set-state-in-effect': 1 } },
      { 'src/a.ts': { 'no-explicit-any': 1 } },
    );
    expect(stale).toEqual([
      { file: 'src/gone.ts', rule: 'no-explicit-any', count: 0, allowed: 2 },
      { file: 'src/gone.ts', rule: 'set-state-in-effect', count: 0, allowed: 1 },
    ]);
    // The unrelated file is new debt, not an excuse to drop the stale report.
    expect(compareToBaseline({ 'src/gone.ts': { 'no-explicit-any': 2 } }, { 'src/a.ts': { 'no-explicit-any': 1 } }).unclassified)
      .toEqual([{ file: 'src/a.ts', rule: 'no-explicit-any', count: 1 }]);
  });
});

describe('missingRationales keeps the ledger self-explaining', () => {
  const violations = { 'src/a.ts': { 'no-explicit-any': 3 } };
  const suppressions = { 'src/b.ts': { 'exhaustive-deps': 1 } };

  it('accepts a ledger where every rule is explained', () => {
    const rationale = { 'no-explicit-any': 'Untyped API payloads.', 'exhaustive-deps': 'Deliberate.' };
    expect(missingRationales([violations, suppressions], rationale)).toEqual([]);
  });

  it('names a rule that reached the ledger without an explanation', () => {
    expect(missingRationales([violations, suppressions], { 'no-explicit-any': 'Untyped API payloads.' }))
      .toEqual(['exhaustive-deps']);
  });

  it('treats an empty explanation as no explanation', () => {
    expect(missingRationales([violations], { 'no-explicit-any': '' })).toEqual(['no-explicit-any']);
  });

  it('names every unexplained rule once, sorted', () => {
    expect(missingRationales([{ 'src/a.ts': { zeta: 1, alpha: 1 }, 'src/b.ts': { alpha: 2 } }, {}], {}))
      .toEqual(['alpha', 'zeta']);
  });
});

describe('fatalProblems catches what the ledger structurally cannot', () => {
  // A parse failure, an invalid config or a plugin that threw is reported with
  // no rule id, so there is no (file, rule) key to baseline it under. Worse, a
  // file that does not parse yields no violations at all, so a pure tally
  // comparison reads it as clean. These have to fail the gate on their own.
  const message = (over = {}) => ({ ruleId: null, severity: 2, line: 1, message: 'Parsing error: ;', ...over });

  it('finds a rule-less error', () => {
    const results = [{ filePath: '/ui/src/broken.ts', messages: [message()] }];
    expect(fatalProblems(results)).toEqual([
      { filePath: '/ui/src/broken.ts', line: 1, message: 'Parsing error: ;' },
    ]);
  });

  it('ignores ordinary rule violations, which the ledger does own', () => {
    const results = [
      { filePath: '/ui/src/a.ts', messages: [{ ruleId: 'no-explicit-any', severity: 2, line: 3, message: 'any' }] },
    ];
    expect(fatalProblems(results)).toEqual([]);
  });

  it('ignores a rule-less warning, which does not gate anything', () => {
    const results = [{ filePath: '/ui/src/a.ts', messages: [message({ severity: 1 })] }];
    expect(fatalProblems(results)).toEqual([]);
  });

  it('tolerates a result with no messages array', () => {
    expect(fatalProblems([{ filePath: '/ui/src/a.ts' }])).toEqual([]);
  });
});

describe('reportLines fails on a fatal error even when every tally matches', () => {
  // The unconditional property. If this ever reduces to "only report drift",
  // a file that stops parsing ships as a green build.
  const clean = { unclassified: [], expanded: [], stale: [] };

  it('reports nothing when the run is genuinely clean', () => {
    expect(
      reportLines({ violationDrift: clean, suppressionDrift: clean, unexplained: [], fatals: [] }),
    ).toEqual([]);
  });

  it('still fails when the only problem is a fatal error', () => {
    const lines = reportLines({
      violationDrift: clean,
      suppressionDrift: clean,
      unexplained: [],
      fatals: [{ filePath: 'src/broken.ts', line: 4, message: 'Parsing error: ;' }],
    });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('FATAL');
    expect(lines[0]).toContain('src/broken.ts');
    expect(lines[0]).toContain('Parsing error: ;');
  });

  it('keeps reporting drift and unexplained rules alongside', () => {
    const lines = reportLines({
      violationDrift: { unclassified: [{ file: 'src/a.ts', rule: 'no-explicit-any', count: 1 }], expanded: [], stale: [] },
      suppressionDrift: clean,
      unexplained: ['mystery-rule'],
      fatals: [{ filePath: 'src/broken.ts', line: 4, message: 'Parsing error: ;' }],
    });
    expect(lines.filter((line) => line.includes('FATAL'))).toHaveLength(1);
    expect(lines.filter((line) => line.includes('NEW'))).toHaveLength(1);
    expect(lines.filter((line) => line.includes('UNEXPLAINED'))).toHaveLength(1);
  });
});

describe('the gate script stays reviewable text', () => {
  const source = fs.readFileSync(fileURLToPath(new URL('./lint-baseline.mjs', import.meta.url)));

  it('contains no raw NUL byte', () => {
    // The (file, rule) key joins on NUL. Written as a literal byte it makes the
    // whole script binary to git, so the gate everyone is asked to trust stops
    // showing up in diffs and review. The escape has identical runtime meaning.
    expect(source.includes(0)).toBe(false);
  });

  it('still joins the pair on a character neither half can contain', () => {
    expect(pairKey('src/a.ts', 'no-explicit-any')).toBe('src/a.ts\0no-explicit-any');
  });

  it('cannot confuse two different pairs that share a prefix', () => {
    expect(pairKey('src/a.ts', 'b/c')).not.toBe(pairKey('src/a.ts b', 'c'));
  });
});

describe('compareToBaseline cannot pass vacuously', () => {
  it('fails when the lint run produced nothing at all', () => {
    // A broken glob or a crashed ESLint run yields an empty report. That must
    // read as "the measurement is gone", never as "the code is clean".
    const { stale } = compareToBaseline({ 'src/a.ts': { 'no-explicit-any': 3 } }, {});
    expect(stale).toHaveLength(1);
  });

  it('reports an empty baseline against an empty run as clean', () => {
    // The one honest empty case: nothing recorded, nothing found.
    expect(compareToBaseline({}, {})).toEqual({ unclassified: [], expanded: [], stale: [] });
  });
});
