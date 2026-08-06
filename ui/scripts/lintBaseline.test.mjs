import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import {
  compareToBaseline,
  fatalProblems,
  integrityLines,
  missingRationales,
  pairKey,
  reportLines,
} from './lint-baseline.mjs';

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

  // The gate is a count ratchet, so a violation deleted at one site and
  // reintroduced at another inside the same (file, rule) pair passes. Recorded
  // as an intended property rather than left implicit: a reader who assumes
  // otherwise is relying on a guarantee this file does not make, and the same
  // trade-off is the one ESLint's own suppressions file makes.
  it('accepts a baselined violation replaced one-for-one within the same pair', () => {
    const ledger = { 'src/a.ts': { 'no-explicit-any': 3 } };
    expect(compareToBaseline(ledger, { 'src/a.ts': { 'no-explicit-any': 3 } })).toEqual({
      unclassified: [],
      expanded: [],
      stale: [],
    });
  });

  // The bound on that residual, in one place: a swap can only ever hide inside
  // debt that is already recorded, at a count that cannot grow, in the file that
  // already carries it.
  it('catches the same swap the moment it changes the count, the rule or the file', () => {
    const ledger = { 'src/a.ts': { 'no-explicit-any': 3 } };
    expect(compareToBaseline(ledger, { 'src/a.ts': { 'no-explicit-any': 4 } }).expanded).toHaveLength(1);
    expect(compareToBaseline(ledger, { 'src/a.ts': { 'no-explicit-any': 3, 'no-empty': 1 } }).unclassified)
      .toHaveLength(1);
    expect(compareToBaseline(ledger, { 'src/b.ts': { 'no-explicit-any': 3 } }).unclassified).toHaveLength(1);
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

describe('reportLines turns recorded-debt drift into a verdict', () => {
  const clean = { unclassified: [], expanded: [], stale: [] };

  it('reports nothing when the run is genuinely clean', () => {
    expect(reportLines({ violationDrift: clean, suppressionDrift: clean, unexplained: [] })).toEqual([]);
  });

  it('reports drift and unexplained rules together', () => {
    const lines = reportLines({
      violationDrift: { unclassified: [{ file: 'src/a.ts', rule: 'no-explicit-any', count: 1 }], expanded: [], stale: [] },
      suppressionDrift: clean,
      unexplained: ['mystery-rule'],
    });
    expect(lines.filter((line) => line.includes('NEW'))).toHaveLength(1);
    expect(lines.filter((line) => line.includes('UNEXPLAINED'))).toHaveLength(1);
  });
});

describe('integrityLines fails on an incomplete measurement even when every tally matches', () => {
  // The unconditional half. `reportLines` can only speak about what was found;
  // everything here is a way the run looked at less than it claims to, which no
  // tally comparison can detect because the tally comes out clean.
  const sound = { fatals: [], coverage: { empty: false, missing: [], unwalked: [] }, policyGroups: [], inlineViolations: [] };

  it('reports nothing when the measurement is complete', () => {
    expect(integrityLines(sound)).toEqual([]);
  });

  it('fails when a file stopped parsing, which yields no violations to count', () => {
    const lines = integrityLines({
      ...sound,
      fatals: [{ filePath: 'src/broken.ts', line: 4, message: 'Parsing error: ;' }],
    });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('FATAL');
    expect(lines[0]).toContain('src/broken.ts');
    expect(lines[0]).toContain('Parsing error: ;');
  });

  it('fails when repo-owned TypeScript was never opened', () => {
    const lines = integrityLines({ ...sound, coverage: { empty: false, missing: ['vite.config.ts'], unwalked: [] } });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('UNLINTED');
    expect(lines[0]).toContain('vite.config.ts');
  });

  it('fails when the domain walk missed a file the run did open', () => {
    const lines = integrityLines({ ...sound, coverage: { empty: false, missing: [], unwalked: ['src/hidden.tsx'] } });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('UNWALKED');
  });

  it('fails when the walk found nothing, instead of passing vacuously', () => {
    const lines = integrityLines({ ...sound, coverage: { empty: true, missing: [], unwalked: [] } });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('DOMAIN');
  });

  it('fails when the effective policy drifted, printing one entry per distinct drift', () => {
    const lines = integrityLines({
      ...sound,
      policyGroups: [
        {
          differences: ['rule config changed — @typescript-eslint/no-explicit-any: expected [2], resolved [1]'],
          files: Array.from({ length: 514 }, (_, index) => `src/f${index}.ts`),
        },
      ],
    });
    // Grouped: a config-wide drift is one finding, not 514 copies of it.
    expect(lines).toHaveLength(2);
    expect(lines[0]).toContain('POLICY    514 file(s)');
    expect(lines[0]).toContain('+511 more');
    expect(lines[1]).toContain('no-explicit-any');
  });

  it('fails when a source rewrote policy inline', () => {
    const lines = integrityLines({
      ...sound,
      inlineViolations: [{ file: 'src/a.ts', line: 1, column: 1, text: '/* eslint no-var: "off" */' }],
    });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('INLINE');
    expect(lines[0]).toContain('src/a.ts:1:1');
  });

  it('reports every kind at once rather than stopping at the first', () => {
    const lines = integrityLines({
      fatals: [{ filePath: 'src/broken.ts', line: 4, message: 'Parsing error: ;' }],
      coverage: { empty: true, missing: ['vite.config.ts'], unwalked: ['src/hidden.tsx'] },
      policyGroups: [{ differences: ['parser: expected "x", resolved "y"'], files: ['src/a.ts'] }],
      inlineViolations: [{ file: 'src/a.ts', line: 1, column: 1, text: '/* eslint no-var: "off" */' }],
    });
    for (const label of ['FATAL', 'DOMAIN', 'UNLINTED', 'UNWALKED', 'POLICY', 'INLINE']) {
      expect(lines.some((line) => line.includes(label)), label).toBe(true);
    }
  });
});

describe('an incomplete measurement blocks --update too, not just the verdict', () => {
  // The ordering has to hold in the real script: `lint:baseline` exists to
  // rewrite the ledger, so if it ran before the integrity checks, any of the
  // failures above could be laundered into a freshly written green baseline.
  // Asserted against the actual command rather than an injected fake, because a
  // fake would only pin the ordering a test author already imagined.
  const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));
  const BASELINE = path.join(UI_ROOT, 'eslint-baseline.json');
  const PROBE = path.join(UI_ROOT, 'update-integrity-probe.ts');

  const runGate = (args) =>
    spawnSync(process.execPath, ['scripts/lint-baseline.mjs', ...args], {
      cwd: UI_ROOT,
      encoding: 'utf8',
      timeout: 300_000,
    });

  it('refuses to write a baseline while an integrity check is failing', () => {
    const before = fs.readFileSync(BASELINE);
    try {
      fs.writeFileSync(PROBE, '/* eslint @typescript-eslint/no-explicit-any: "off" */\nexport const f = (v: any) => v;\n');
      const result = runGate(['--update']);
      expect(result.status).toBe(1);
      expect(result.stderr).toContain('INLINE');
      expect(result.stderr).toContain('update-integrity-probe.ts');
      expect(fs.readFileSync(BASELINE).equals(before)).toBe(true);
    } finally {
      fs.rmSync(PROBE, { force: true });
      fs.writeFileSync(BASELINE, before);
    }
  }, 300_000);
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
