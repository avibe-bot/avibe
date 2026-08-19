import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterAll, describe, expect, it } from 'vitest';

import { collectCases, isUiEvidence, resolveUiEvidence, uiEvidenceRows } from './scenarioCatalog.mjs';

/** Executable, and so citable as evidence. */
const RUNS = 'MH-FIXTURE-RUNS';
/** Present in the file, executed by nothing, and so citable by nothing. */
const DEAD = 'MH-FIXTURE-DEAD';

const FIXTURE_FILE = 'probe.test.tsx';

/**
 * One declaration of every shape a test file can carry, each named for whether
 * vitest runs it. The assertion below reads those names instead of a list of
 * cases, so a shape added here is covered without editing it.
 *
 * The shapes a source scan got wrong are here: a regex literal opening after a
 * keyword, a `describe.skip` body, an unreachable branch, and a name quoted
 * inside a literal. `list` collects by importing, so a shape whose declaration
 * never executes is absent from the collection for the same reason the suite
 * never runs it — which is why the one remaining scan-only shape, a name written
 * as JSX text, needs no seed here: it is not a declaration, and this fixture
 * cannot resolve the React runtime from a temp directory anyway.
 */
const FIXTURE = `
describe('resolver fixture', () => {
  // it('${DEAD}-COMMENTED: commented out', () => {});
  /* it('${DEAD}-BLOCK: commented out in a block', () => {}); */
  it.skip('${DEAD}-SKIPPED: declared with a modifier', () => {});
  it.each([1, 2])('${DEAD}-EACH: named at run time %i', () => {});
  it('${RUNS}-QUOTES: quotes declarations and matches on apostrophes', () => {
    expect(label).toBe("it('${DEAD}-INSTRING: quoted in a string', () => {})");
    expect(hint).toBe(\`it('${DEAD}-INTEMPLATE: quoted in a template', \${suffix})\`);
    expect(pattern(() => { return /it('${DEAD}-REGEX: held in a regex literal', () => {})/ })).toBe(true);
    expect(text).toMatch(/Couldn't refresh, please retry/i);
    expect(share).toBe(total / count);
  });
  it('${RUNS}-AFTER: runs after every shape above', () => {
    expect(true).toBe(true);
  });
});

describe.skip('skipped suite', () => {
  it('${DEAD}-INSKIPPEDSUITE: declared inside a skipped suite', () => {});
});

if (false) {
  it('${DEAD}-UNREACHABLE: declared in a branch that never runs', () => {});
}
`;

const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'mh-scenario-catalog-'));
fs.writeFileSync(path.join(directory, FIXTURE_FILE), FIXTURE);
afterAll(() => fs.rmSync(directory, { recursive: true, force: true }));

/** Every ID the fixture mentions, in any shape, deduplicated. */
const fixtureIds = [...new Set([...FIXTURE.matchAll(/MH-FIXTURE-(?:RUNS|DEAD)-[A-Z]+/g)].map(([id]) => id))];

describe('UI evidence resolution', () => {
  // The failure this closes is the one a text search cannot tell apart from
  // evidence: a catalog ID that appears in a `.tsx` file vitest executes no
  // matching case from, leaving a row `covered` by text alone.
  it(
    'MH-CATALOG-002: a UI-evidenced row resolves only to a vitest case that runs under its catalog ID',
    () => {
      // `--globals` because the fixture lives outside this package and cannot
      // resolve `vitest` from there; collection is otherwise the CI call.
      const collected = collectCases({ root: directory, globals: true });
      const resolves = (id) => resolveUiEvidence([{ id, cited: id, file: FIXTURE_FILE }], collected).length === 0;

      // The IDs that resolve are exactly the ones the fixture names as running —
      // not a list of the shapes that must fail, which would pass forever while
      // the one shape nobody named went on resolving.
      expect(fixtureIds.filter(resolves).sort()).toEqual(fixtureIds.filter((id) => id.startsWith(RUNS)).sort());
    },
    60_000,
  );

  it('reports a row whose evidence is absent, duplicated, or in another file', () => {
    const collected = [
      { name: 'suite > MH-ROW-A: runs', file: `/checkout/${FIXTURE_FILE}` },
      { name: 'suite > MH-ROW-B: runs, but in another file', file: '/checkout/other.test.tsx' },
      { name: 'suite > MH-ROW-C: runs', file: `/checkout/${FIXTURE_FILE}` },
      { name: 'suite > MH-ROW-C: runs again', file: `/checkout/${FIXTURE_FILE}` },
      { name: 'TaskDetail > command task > states the timeout', file: `/checkout/${FIXTURE_FILE}` },
      { name: 'suite > [MH-ROW-X][MH-ROW-F] one case answering two rows', file: `/checkout/${FIXTURE_FILE}` },
    ];
    const row = (id, extra = {}) => ({ id, cited: id, file: FIXTURE_FILE, ...extra });

    expect(resolveUiEvidence([row('MH-ROW-A')], collected)).toEqual([]);
    // Present in the collection, but not in the file the row points at.
    expect(resolveUiEvidence([row('MH-ROW-B')], collected)).toHaveLength(1);
    expect(resolveUiEvidence([row('MH-ROW-C')], collected)).toHaveLength(1);
    expect(resolveUiEvidence([row('MH-ROW-D')], collected)).toHaveLength(1);
    // A row may only resolve through evidence named for itself, so it cannot pass
    // by citing a sibling's — which is a citation that does resolve, to a case
    // that is another row's answer.
    const ids = new Set(['MH-ROW-A', 'MH-ROW-D']);
    expect(resolveUiEvidence([row('MH-ROW-D', { cited: 'MH-ROW-A' })], collected, ids)).toHaveLength(1);
    // The other convention in the checkout: the case's readable full name, which
    // the catalogs write with vitest's separator flattened to a space.
    expect(resolveUiEvidence([row('SCT-018', { cited: 'TaskDetail command task states the timeout' })], collected)).toEqual([]);
    // An ID the case name carries without leading with it, which is why the rule
    // is containment: one case can answer two scenarios, and each of them cites
    // it by its own ID.
    expect(resolveUiEvidence([row('MH-ROW-F', { cited: '[MH-ROW-F]' })], collected)).toEqual([]);
    // A citation naming a file and no case in it is evidence for no row in
    // particular — whatever else cites the file keeps it collected, so the row
    // would read `covered` with nothing tied to itself. Reported, not resolved.
    expect(resolveUiEvidence([row('MH-ROW-E', { cited: null })], collected)).toHaveLength(1);
  });

  it('resolves a parameterized case through the inputs the citation names', () => {
    // A case named at run time has no name to cite, so the catalog writes the
    // template and its substitutions in separate keys. Both are terms of one
    // citation, and the count is still what decides.
    const collected = [
      { name: 'parse > accepts the declared failure memory_repair_failed with result failed', file: '/checkout/parse.test.ts' },
      { name: 'parse > accepts the declared failure memory_repair_failed with result timed_out', file: '/checkout/parse.test.ts' },
    ];
    const cited = (inputs) => [{
      id: 'MEMORY-REPAIR-004',
      file: 'parse.test.ts',
      cited: 'accepts the declared failure %s with result %s',
      inputs,
    }];

    expect(resolveUiEvidence(cited(['memory_repair_failed', 'timed_out']), collected)).toEqual([]);
    // The template alone reaches every row of the table, which identifies none of
    // them — the same failure as reaching no case at all.
    expect(resolveUiEvidence(cited([]), collected)).toHaveLength(1);
  });

  it('asks about every UI citation a row makes, whatever key it sits in and whatever its legend looks like', () => {
    // Two holes of one shape, each found a round apart. The legend: `status_legend`
    // is a description string in every catalog but one, so selecting rows by
    // `test_required` asked about that catalog and passed the rest without saying
    // so. The key: reading `test` alone left every `ui_contract` unasked, and four
    // of the five in the checkout named a case that no longer existed. A citation
    // is the row's own claim, so the answer to both is to read them all.
    const scenarios = [
      { id: 'MH-UI', status: 'covered', test: 'ui/src/a.test.tsx::MH-UI' },
      { id: 'MH-SCRIPT', status: 'covered', test: 'ui/scripts/a.test.mjs::MH-SCRIPT' },
      { id: 'MH-FILE', status: 'covered', test: 'ui/src/b.test.tsx' },
      { id: 'MH-PY', status: 'covered', test: 'tests/test_a.py::test_a' },
      {
        id: 'MH-CONTRACT',
        status: 'covered',
        test: 'tests/test_b.py::test_b',
        ui_contract: { test: 'ui/src/c.test.tsx', case: 'holds the frozen contract' },
      },
      // A pointer, not a claim: it names a file and no case, so it evidences no
      // row and does not say it does. Unread for the same reason a file-only
      // citation is rejected, rather than as an exception to it.
      { id: 'MH-RELATED', status: 'covered', test: 'tests/test_c.py::test_c', related_tests: ['ui/src/d.test.tsx'] },
      { id: 'MH-GAP', status: 'gap', test: null },
    ];
    const asked = ['MH-UI', 'MH-SCRIPT', 'MH-FILE', 'MH-CONTRACT'];
    const rowsOf = (catalog) => uiEvidenceRows({ ...catalog, scenarios });

    expect(rowsOf({ status_legend: { covered: { test_required: true } } }).map((row) => row.id)).toEqual(asked);
    expect(rowsOf({ status_legend: { covered: 'executable evidence exists' } }).map((row) => row.id)).toEqual(asked);
    expect(rowsOf({}).map((row) => row.id)).toEqual(asked);
    // The shape the resolver then rejects, and the one it reads a case out of.
    expect(rowsOf({}).find((row) => row.id === 'MH-FILE').cited).toBe(null);
    expect(rowsOf({}).find((row) => row.id === 'MH-CONTRACT')).toMatchObject({
      file: 'ui/src/c.test.tsx',
      cited: 'holds the frozen contract',
    });
    expect(isUiEvidence('tests/test_a.py::test_a')).toBe(false);
  });
});
