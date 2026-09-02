// The boundary that keeps the restoration class closed.
//
// Two review rounds went to one defect in three costumes: a spec deciding for
// itself what to put back. Every site answered with a proxy — "whatever appeared
// since I looked", "`enabled` is false", "the chain as I found it" — and every
// proxy was right about the wrong thing. `support/restore.ts` now answers those
// questions once, which repairs the sites that exist. This repairs the next one.
//
// The rule is a boundary, not a style. A spec ARRANGES state: it may PUT a
// chain, flip a mode, delete a source it made. What it may not do is READ the
// state it owes back, because that read is a capture, a capture is one half of a
// restoration contract, and a capture written beside the arrangement is exactly
// how the two halves drift apart — the g-guards baseline kept a hop the spec was
// about to delete, and `set_agent_chain` refuses a whole PUT over one dead hop.
//
// Not a browser test: no fixture is requested, so nothing launches. It runs
// where the files it judges run.
import { readdirSync, readFileSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';

const E2E_DIR = dirname(fileURLToPath(import.meta.url));
const SELF = basename(fileURLToPath(import.meta.url));

/** What a spec may not reach for, and the declaration that owns it instead. */
const RESTORE_ONLY = [
  {
    needle: 'api.chains(',
    owner: 'captureAgentChain',
    why: 'a baseline read beside the arrangement is the one that keeps hops the spec itself deletes',
  },
  {
    needle: 'supply_channel',
    owner: 'restoreNativeSources',
    why: 'what a native source IS has to have one answer, not one per teardown',
  },
];

test('specs arrange state; support/restore.ts is what reads it back', () => {
  // Self-excluded, because the needles are spelled out above. Nothing else is:
  // a new spec is judged the moment it lands in this directory.
  const specs = readdirSync(E2E_DIR).filter((name) => name.endsWith('.spec.ts') && name !== SELF);
  expect(
    specs.length,
    'No sibling spec files were found, so this guard would pass over an empty suite.',
  ).toBeGreaterThan(0);

  const offences = specs.flatMap((name) => {
    const source = readFileSync(join(E2E_DIR, name), 'utf8');
    return RESTORE_ONLY.filter((rule) => source.includes(rule.needle)).map(
      (rule) => `${name} reaches for \`${rule.needle}\` — use \`${rule.owner}\` from support/restore.ts: ${rule.why}.`,
    );
  });

  expect(offences, offences.join('\n')).toEqual([]);
});
