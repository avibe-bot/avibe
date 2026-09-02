// The boundaries that keep this suite's closed defect classes closed.
//
// Four review rounds went to four classes, and each one arrived as a handful of
// sites that had independently answered the same question — what to put back,
// which surface this instance renders, what a selector's value may contain,
// what a negation means. Every answer was a PROXY: right about the wrong thing,
// and right often enough to look like a convention rather than a defect.
// `support/` now answers each of them once, which repairs the sites that exist.
// This repairs the next one.
//
// A rule here is a tripwire on the exact spelling the class used, not a linter.
// It cannot prove the absence of a defect; what it does is make the sixth member
// of a closed class fail a test instead of costing a review round.
//
// Not a browser test: no fixture is requested, so nothing launches. It runs
// where the files it judges run.
import { readdirSync, readFileSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from '@playwright/test';

import { type Agent, remoteAuthRefusal, type Source, surfaceKind } from './support/api';

const E2E_DIR = dirname(fileURLToPath(import.meta.url));
const SUPPORT_DIR = join(E2E_DIR, 'support');
const SELF = basename(fileURLToPath(import.meta.url));

type Rule = {
  /** What the class looked like when it was written by hand. */
  needle: string | RegExp;
  /** The declaration in `support/` that owns the answer instead. */
  owner: string;
  why: string;
  /** `support` files that legitimately contain the needle — the owner's own. */
  allow?: string[];
};

/** Rules judged over the spec files: what a spec may not decide for itself. */
const SPEC_RULES: Rule[] = [
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
  {
    needle: 'api.startRuntime(',
    owner: 'withRuntimeRestored',
    why: 'a restoration written beside the stop is a boundary that opens after the request has already gone',
  },
  {
    needle: "mode === 'direct'",
    owner: 'surfaceKind',
    why: 'every-backend-direct is only the OUTER of the two tests the product makes, so alone it admits an instance whose page is a different shape',
  },
  {
    needle: /\.not\.(toContainText|toHaveText)\(/,
    owner: 'expectVisibleWithout',
    why: 'a negated text assertion is satisfied by an element that is not there at all, which is the opposite verdict',
  },
];

/** Rules judged over the page object and the specs alike: a selector is built
 *  in one place, but a spec reaching past it would have the same problem. */
const SELECTOR_RULES: Rule[] = [
  {
    needle: /\.locator\(\s*`/,
    owner: 'attr',
    why: 'a route row is keyed by an operator-chosen model id, and a quote in it either breaks the selector or silently moves it',
    allow: ['hub.ts'],
  },
];

const offencesIn = (dir: string, files: string[], rules: Rule[]): string[] =>
  files.flatMap((name) => {
    const source = readFileSync(join(dir, name), 'utf8');
    return rules
      .filter((rule) => !rule.allow?.includes(name))
      .filter((rule) => (typeof rule.needle === 'string' ? source.includes(rule.needle) : rule.needle.test(source)))
      .map((rule) => `${name} reaches for \`${rule.needle}\` — use \`${rule.owner}\` from support/: ${rule.why}.`);
  });

test('the suite asks support/ its closed questions', () => {
  // Self-excluded, because the needles are spelled out above. Nothing else is:
  // a new spec is judged the moment it lands in this directory.
  const specs = readdirSync(E2E_DIR).filter((name) => name.endsWith('.spec.ts') && name !== SELF);
  const support = readdirSync(SUPPORT_DIR).filter((name) => name.endsWith('.ts'));
  expect(
    specs.length,
    'No sibling spec files were found, so this guard would pass over an empty suite.',
  ).toBeGreaterThan(0);
  expect(
    support.length,
    'No support files were found, so the selector rule would pass over an empty directory.',
  ).toBeGreaterThan(0);

  const offences = [
    ...offencesIn(E2E_DIR, specs, [...SPEC_RULES, ...SELECTOR_RULES]),
    ...offencesIn(SUPPORT_DIR, support, SELECTOR_RULES),
  ];

  expect(offences, offences.join('\n')).toEqual([]);
});

const agent = (mode: 'hub' | 'direct', cliPresent: boolean): Agent =>
  ({ backend: 'claude', mode, cli_present: cliPresent });

test('surfaceKind separates the two direct surfaces, not just direct from gateway', () => {
  const noSources: Source[] = [];
  const oneSource = [{ display_name: 'anything' } as Source];

  // The pair the rule above exists for: identical on the outer test, different
  // pages. The second renders `.model-hub-direct-empty` — no backend card, no
  // way out of Direct mode — so a spec asserting the card there fails on an
  // instance that is merely bare, which is a documented skip.
  expect(surfaceKind([agent('direct', true)], noSources)).toBe('direct-home');
  expect(surfaceKind([agent('direct', false)], noSources)).toBe('direct-no-backend');

  // And the outer test itself, from either side.
  expect(surfaceKind([agent('hub', true)], noSources)).toBe('gateway');
  expect(surfaceKind([agent('direct', true)], oneSource)).toBe('gateway');
  // An instance with no agents at all is bare, not a gateway.
  expect(surfaceKind([], noSources)).toBe('direct-no-backend');
});

test('a remote-access refusal is answered with what to do, not with a status', () => {
  // Four names, one thing the operator can do about any of them. Matching the
  // family rather than the member is what keeps a fifth from arriving as a bare
  // 401 — the product owns this list, and the suite does not get told when it
  // grows.
  for (const error of [
    'remote_access_login_required',
    'remote_access_authorization_refresh_required',
    'remote_access_revoked',
    'remote_access_authorization_unavailable',
  ]) {
    const message = remoteAuthRefusal(401, JSON.stringify({ ok: false, error }));
    expect(message, error).toContain('no login step');
    expect(message, error).toContain(error);
  }

  // And nothing else is one. A 401 from somewhere else, or any other status,
  // still owes the caller the status and the body the generic throw carries.
  expect(remoteAuthRefusal(401, JSON.stringify({ error: 'csrf_token_invalid' }))).toBeNull();
  expect(remoteAuthRefusal(401, 'Unauthorized')).toBeNull();
  expect(remoteAuthRefusal(500, JSON.stringify({ error: 'remote_access_revoked' }))).toBeNull();
});
