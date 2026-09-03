import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * Every way out of the model picker settles it through one call.
 *
 * `addCandidates` is where a picker exit's two consequences are decided: which
 * seeded ids the user has just declined — so a projection the server refused
 * stops being sendable — and which row an accepted id lands as, resolved id
 * included. Two exits went through it and a third, the custom-model hand-off,
 * reproduced half of it by hand. That is how a re-ask's refused promise survived
 * an exit, and how a typed id was asked about before it had been resolved: one
 * structural fact, reported as two defects.
 *
 * So the exits are read out of the source rather than listed here. The list is
 * the thing that failed: it reads as complete, and the door added next is absent
 * from it by construction. Taking the enumeration from the JSX makes a fourth
 * exit fail this test instead of costing a review round.
 *
 * A callback that genuinely settles nothing would trip this too, and that is the
 * intended cost: routing it through the call is one line, while the alternative
 * is a door whose consequences nobody stated.
 */
const CHOKEPOINT = 'addCandidates';
const MOUNT = '<BackendModelPickerDialog';

describe("the model picker's exits", () => {
  /** The mounted element, comments stripped — prose naming an exit is not one. */
  const mounted = (): string => {
    const source = readFileSync(join(__dirname, 'BackendModelCatalogDialog.tsx'), 'utf8');
    const start = source.indexOf(MOUNT);
    expect(start).toBeGreaterThan(-1);
    const lines = source.slice(start).split('\n');
    const close = lines.findIndex((line) => line.trim() === '/>');
    // A mount this cannot delimit fails here rather than passing vacuously: the
    // assertion below holds for an empty region no matter what the file says.
    expect(close).toBeGreaterThan(0);
    return lines
      .slice(0, close + 1)
      .filter((line) => !/^(?:\/\/|\/?\*)/.test(line.trim()))
      .join('\n');
  };

  it('settles every exit it is mounted with through the one call', () => {
    const element = mounted();
    const exits = element
      .split(/(?=\bon[A-Z]\w*=)/)
      .filter((chunk) => /^on[A-Z]\w*=/.test(chunk));
    // The scan has to find doors, or it asserts nothing about them.
    expect(exits.length).toBeGreaterThan(0);

    const bypassing = exits
      .filter((chunk) => !chunk.includes(CHOKEPOINT))
      .map((chunk) => chunk.slice(0, chunk.indexOf('=')));

    expect(bypassing).toEqual([]);
  });
});
