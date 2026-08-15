// The lint domain and the policy that applies across it, as one reviewable
// subject shared by the gate (``scripts/lint-baseline.mjs``) and its tests.
//
// ``eslint-baseline.json`` records what ESLint *found*, never what it was asked
// to *look at*. Anything that shrinks the second — a narrowed scope, a demoted
// rule, a swapped parser, an inline override — pays debt down on paper while the
// ledger stays green. Earlier revisions tried to close that with hand-written
// projections of the config (the warning ids, the disabled ids, a union of
// ``files``, each entry's ``{name, files, ignores}``), and every one of them had
// a blind complement: ``basePath`` narrows an entry's reach without touching any
// field those projections listed.
//
// So the domain is not enumerated here at all. It is *measured*: every
// repo-owned ``.ts``/``.tsx`` file under ``ui/``, found by walking the tree,
// minus only the two roots the declared policy already places outside it
// (``node_modules``, ``dist``). Each of those files must appear in the ESLint run
// and must resolve exactly ``EXPECTED_POLICY``. A new nested source file, a new
// root-level config script, a directory nobody remembered — all measured, with
// nothing to keep in sync.
//
// ``EXPECTED_POLICY`` is the full resolved configuration, not a projection of it:
// every rule with its options, plus the boundaries that can stop a rule running
// even when the rule map is intact (parser identity, ECMAScript and module
// settings, processor, plugin ids, linter options, globals). A dependency upgrade
// is meant to fail here — the diff is the review prompt, and it is the only place
// a change in enforcement policy becomes visible before it silently changes what
// the ledger is allowed to record.

import fs from 'node:fs';
import path from 'node:path';

import { EXPECTED_BROWSER_GLOBALS } from './browserGlobals.mjs';

/**
 * Directory names the declared lint policy already places outside the domain:
 * ``node_modules`` is not repo-owned, and ``dist`` is build output that
 * ``eslint.config.js`` globally ignores. Nothing else may be excluded — an
 * exclusion here is a hole in the invariant, so the list is meant to stay at two.
 */
export const DEPENDENCY_ROOTS = ['node_modules', 'dist'];

/** The extensions the shared config's rule-bearing entry claims. */
export const INTENDED_EXTENSIONS = ['.ts', '.tsx'];

/**
 * Every rule the shared config resolves for a TypeScript source, with its
 * options, sorted by rule id.
 *
 * Severity alone was not enough: a preset that keeps a rule at error while
 * loosening its options enforces less and reads as unchanged. The value here is
 * exactly what ``calculateConfigForFile`` returns, so the comparison is equality
 * against the whole thing.
 *
 * Level 0 is not "we switched it off". It is a base ESLint rule that
 * ``typescript-eslint``'s recommended preset disables because a type-aware
 * equivalent supersedes it.
 */
export const EXPECTED_RULES = {
  '@typescript-eslint/ban-ts-comment': [2],
  '@typescript-eslint/no-array-constructor': [2],
  '@typescript-eslint/no-duplicate-enum-values': [2],
  '@typescript-eslint/no-empty-object-type': [2],
  '@typescript-eslint/no-explicit-any': [2],
  '@typescript-eslint/no-extra-non-null-assertion': [2],
  '@typescript-eslint/no-misused-new': [2],
  '@typescript-eslint/no-namespace': [2],
  '@typescript-eslint/no-non-null-asserted-optional-chain': [2],
  '@typescript-eslint/no-require-imports': [2],
  '@typescript-eslint/no-this-alias': [2],
  '@typescript-eslint/no-unnecessary-type-constraint': [2],
  '@typescript-eslint/no-unsafe-declaration-merging': [2],
  '@typescript-eslint/no-unsafe-function-type': [2],
  '@typescript-eslint/no-unused-expressions': [2, { allowShortCircuit: false, allowTaggedTemplates: false, allowTernary: false }],
  // The `^_` opt-out convention the cleanup standardised on. Behaviourally
  // probed in `eslintConventions.test.mjs`; pinned as configuration here.
  '@typescript-eslint/no-unused-vars': [2, { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_', destructuredArrayIgnorePattern: '^_' }],
  '@typescript-eslint/no-wrapper-object-types': [2],
  '@typescript-eslint/prefer-as-const': [2],
  '@typescript-eslint/prefer-namespace-keyword': [2],
  '@typescript-eslint/triple-slash-reference': [2],
  'constructor-super': [0],
  'for-direction': [2],
  'getter-return': [0, { allowImplicit: false }],
  'no-array-constructor': [0],
  'no-async-promise-executor': [2],
  'no-case-declarations': [2],
  'no-class-assign': [0],
  'no-compare-neg-zero': [2],
  'no-cond-assign': [2, 'except-parens'],
  'no-const-assign': [0],
  'no-constant-binary-expression': [2],
  'no-constant-condition': [2, { checkLoops: 'allExceptWhileTrue' }],
  'no-control-regex': [2],
  'no-debugger': [2],
  'no-delete-var': [2],
  'no-dupe-args': [0],
  'no-dupe-class-members': [0],
  'no-dupe-else-if': [2],
  'no-dupe-keys': [0],
  'no-duplicate-case': [2],
  'no-empty': [2, { allowEmptyCatch: false }],
  'no-empty-character-class': [2],
  'no-empty-pattern': [2, { allowObjectPatternsAsParameters: false }],
  'no-empty-static-block': [2],
  'no-ex-assign': [2],
  'no-extra-boolean-cast': [2, {}],
  'no-fallthrough': [2, { allowEmptyCase: false, reportUnusedFallthroughComment: false }],
  'no-func-assign': [0],
  'no-global-assign': [2, { exceptions: [] }],
  'no-import-assign': [0],
  'no-invalid-regexp': [2, {}],
  'no-irregular-whitespace': [2, { skipComments: false, skipJSXText: false, skipRegExps: false, skipStrings: true, skipTemplates: false }],
  'no-loss-of-precision': [2],
  'no-misleading-character-class': [2, { allowEscape: false }],
  'no-new-native-nonconstructor': [0],
  'no-new-symbol': [0],
  'no-nonoctal-decimal-escape': [2],
  'no-obj-calls': [0],
  'no-octal': [2],
  'no-prototype-builtins': [2],
  'no-redeclare': [0, { builtinGlobals: true }],
  'no-regex-spaces': [2],
  'no-self-assign': [2, { props: true }],
  'no-setter-return': [0],
  'no-shadow-restricted-names': [2, { reportGlobalThis: false }],
  'no-sparse-arrays': [2],
  'no-this-before-super': [0],
  'no-undef': [0, { typeof: false }],
  'no-unexpected-multiline': [2],
  'no-unreachable': [0],
  'no-unsafe-finally': [2],
  'no-unsafe-negation': [0, { enforceForOrderingRelations: false }],
  'no-unsafe-optional-chaining': [2, { disallowArithmeticOperators: false }],
  'no-unused-expressions': [0, { allowShortCircuit: false, allowTernary: false, allowTaggedTemplates: false, enforceForJSX: false, ignoreDirectives: false }],
  'no-unused-labels': [2],
  'no-unused-private-class-members': [2],
  'no-unused-vars': [0],
  'no-useless-backreference': [2],
  'no-useless-catch': [2],
  'no-useless-escape': [2, { allowRegexCharacters: [] }],
  'no-var': [2],
  'no-with': [0],
  'prefer-const': [2, { destructuring: 'any', ignoreReadBeforeAssign: false }],
  'prefer-rest-params': [2],
  'prefer-spread': [2],
  'react-hooks/component-hook-factories': [2],
  'react-hooks/config': [2],
  'react-hooks/error-boundaries': [2],
  'react-hooks/exhaustive-deps': [1], // deliberate: the remaining warnings are ledgered, not fixed
  'react-hooks/gating': [2],
  'react-hooks/globals': [2],
  'react-hooks/immutability': [2],
  'react-hooks/incompatible-library': [1], // deliberate: upstream advisory about libraries the compiler cannot analyse
  'react-hooks/preserve-manual-memoization': [2],
  'react-hooks/purity': [2],
  'react-hooks/refs': [2],
  'react-hooks/rules-of-hooks': [2],
  'react-hooks/set-state-in-effect': [2],
  'react-hooks/set-state-in-render': [2],
  'react-hooks/static-components': [2],
  'react-hooks/unsupported-syntax': [1], // deliberate: upstream advisory about syntax the compiler cannot analyse
  'react-hooks/use-memo': [2],
  'react-refresh/only-export-components': [2, { allowConstantExport: true }],
  'require-yield': [2],
  'use-isnan': [2, { enforceForIndexOf: false, enforceForSwitchCase: true }],
  'valid-typeof': [2, { requireStringLiterals: false }],
};

/**
 * The whole effective policy an intended file must resolve.
 *
 * Everything outside ``rules`` is a boundary that can stop rules running while
 * the rule map still looks right: a parser that cannot see TypeScript reports
 * nothing, a processor rewrites what the rules are handed, a missing plugin id
 * makes its rules unresolvable, ``linterOptions`` decides whether unused
 * suppressions are surfaced, and ``globals`` decides which identifiers exist.
 *
 * Every field is a committed value. ``globals`` is the one that could not be
 * written inline — see ``./browserGlobals.mjs`` for why it is a snapshot and not
 * a read of the same dependency the resolved config derives from.
 */
export const EXPECTED_POLICY = {
  rules: EXPECTED_RULES,
  parser: 'typescript-eslint/parser',
  ecmaVersion: 2020,
  sourceType: 'module',
  parserOptions: {},
  processor: null,
  plugins: ['@', '@typescript-eslint', 'react-hooks', 'react-refresh'],
  linterOptions: { reportUnusedDisableDirectives: 1 },
  globals: EXPECTED_BROWSER_GLOBALS,
};

/**
 * Every repo-owned ``.ts``/``.tsx`` file under ``root``, relative and sorted.
 *
 * Dirent-based so nothing has to be listed by hand. Symlink handling mirrors
 * ESLint's own traversal exactly, because the two lists are compared in both
 * directions and any disagreement would read as a coverage failure that no edit
 * can fix: a symlinked *file* is linted and so is walked, a symlinked
 * *directory* is not descended into by either side. That also means the walk has
 * no cycle to guard against.
 *
 * ``extensions`` exists so a non-lint gate can measure a different domain over
 * the same tree — ``scripts/validate-theme.mjs`` reads stylesheets too. It
 * defaults to the lint domain, so the invariant this module is named for is
 * unaffected: only a caller that asks for something else gets something else.
 */
export function intendedFiles(root, { excluded = DEPENDENCY_ROOTS, extensions = INTENDED_EXTENSIONS } = {}) {
  const found = [];

  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!excluded.includes(entry.name)) walk(full);
      } else if (extensions.some((extension) => entry.name.endsWith(extension))) {
        found.push(path.relative(root, full).split(path.sep).join('/'));
      }
    }
  };

  walk(root);
  return found.sort();
}

/**
 * Project a resolved ESLint config into the shape ``EXPECTED_POLICY`` pins.
 *
 * ``null`` in, ``null`` out: ``calculateConfigForFile`` returns nothing for a
 * path no config entry matches, which is precisely the failure a scope narrowing
 * produces. Pure.
 */
export function effectivePolicyOf(config) {
  if (!config) return null;
  const languageOptions = config.languageOptions ?? {};
  return {
    rules: config.rules ?? {},
    parser: languageOptions.parser?.meta?.name ?? languageOptions.parser?.name ?? null,
    ecmaVersion: languageOptions.ecmaVersion ?? null,
    sourceType: languageOptions.sourceType ?? null,
    parserOptions: languageOptions.parserOptions ?? {},
    processor: config.processor ?? null,
    plugins: Object.keys(config.plugins ?? {}).sort(),
    linterOptions: config.linterOptions ?? {},
    globals: languageOptions.globals ?? {},
  };
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const preview = (ids, limit = 5) =>
  ids.length <= limit ? ids.join(', ') : `${ids.slice(0, limit).join(', ')}, +${ids.length - limit} more`;

/**
 * Every way ``actual`` departs from ``expected``, as printable lines.
 *
 * Empty means the file resolves the pinned policy exactly. Sorted and
 * deterministic, so identical drift on many files produces one identical
 * signature to group on. Pure.
 */
export function policyDifferences(actual, expected = EXPECTED_POLICY) {
  if (actual === null || actual === undefined) {
    return ['no ESLint configuration matches this file, so nothing about it is linted'];
  }

  const lines = [];

  const missing = Object.keys(expected.rules).filter((rule) => !(rule in actual.rules));
  const unexpected = Object.keys(actual.rules).filter((rule) => !(rule in expected.rules));
  const changed = Object.keys(expected.rules)
    .filter((rule) => rule in actual.rules && !same(actual.rules[rule], expected.rules[rule]))
    .map((rule) => `${rule}: expected ${JSON.stringify(expected.rules[rule])}, resolved ${JSON.stringify(actual.rules[rule])}`);
  if (missing.length > 0) lines.push(`${missing.length} pinned rule(s) no longer resolve: ${preview(missing.sort())}`);
  if (unexpected.length > 0) lines.push(`${unexpected.length} unpinned rule(s) resolve: ${preview(unexpected.sort())}`);
  for (const line of changed.sort()) lines.push(`rule config changed — ${line}`);

  for (const key of ['parser', 'ecmaVersion', 'sourceType', 'processor']) {
    if (!same(actual[key], expected[key])) {
      lines.push(`${key}: expected ${JSON.stringify(expected[key])}, resolved ${JSON.stringify(actual[key])}`);
    }
  }
  for (const key of ['parserOptions', 'plugins', 'linterOptions']) {
    if (!same(actual[key], expected[key])) {
      lines.push(`${key}: expected ${JSON.stringify(expected[key])}, resolved ${JSON.stringify(actual[key])}`);
    }
  }

  const globalsMissing = Object.keys(expected.globals).filter((name) => !(name in actual.globals));
  const globalsExtra = Object.keys(actual.globals).filter((name) => !(name in expected.globals));
  const globalsChanged = Object.keys(expected.globals).filter(
    (name) => name in actual.globals && actual.globals[name] !== expected.globals[name],
  );
  if (globalsMissing.length > 0) lines.push(`${globalsMissing.length} expected global(s) absent: ${preview(globalsMissing.sort())}`);
  if (globalsExtra.length > 0) lines.push(`${globalsExtra.length} unexpected global(s): ${preview(globalsExtra.sort())}`);
  if (globalsChanged.length > 0) lines.push(`${globalsChanged.length} global(s) changed writability: ${preview(globalsChanged.sort())}`);

  return lines;
}

/**
 * Fold per-file differences into one entry per distinct signature.
 *
 * A config-wide drift affects every intended file identically; printing it 514
 * times buries the one thing a reader needs. Pure.
 */
export function groupByDifferences(perFile) {
  const groups = new Map();
  for (const { file, differences } of perFile) {
    if (differences.length === 0) continue;
    const key = JSON.stringify(differences);
    const group = groups.get(key) ?? { differences, files: [] };
    group.files.push(file);
    groups.set(key, group);
  }
  return [...groups.values()]
    .map((group) => ({ differences: group.differences, files: group.files.slice().sort() }))
    .sort((a, b) => (a.files[0] < b.files[0] ? -1 : 1));
}

/**
 * Compare the measured lint domain with what ESLint actually opened.
 *
 * Both directions matter. ``missing`` is a file the invariant claims and the run
 * never saw — the scope narrowing this whole module exists to catch. ``unwalked``
 * is the reverse: ESLint opened a TypeScript file the walk did not produce, which
 * means the walk itself is blind and every other check here is measuring less
 * than it claims. ``empty`` is the vacuous case — no intended files at all — which
 * would otherwise make the invariant pass by measuring nothing. Pure.
 */
export function coverageGaps({ intended, lintedFiles }) {
  const walked = new Set(intended);
  const linted = new Set(lintedFiles);
  return {
    empty: intended.length === 0,
    missing: intended.filter((file) => !linted.has(file)),
    unwalked: [...linted]
      .filter((file) => INTENDED_EXTENSIONS.some((extension) => file.endsWith(extension)))
      .filter((file) => !walked.has(file))
      .sort(),
  };
}

/**
 * The shared config with every rule removed, for a parse-only pass.
 *
 * Keeps the real parser, the real ``files`` patterns and the real language
 * options — the only thing dropped is rule execution, because this pass exists to
 * read comments, not to report violations. Pure.
 */
export const withoutRules = (config) =>
  config.map((entry) => (entry && typeof entry === 'object' && 'rules' in entry ? { ...entry, rules: {} } : entry));

/**
 * Inline configuration comments that are not suppressions.
 *
 * ``eslint-disable`` and its family are accounted for: the gate freezes them in
 * the ledger's ``suppressions`` tally. Every other inline directive escapes both
 * tallies — an ``eslint rule-id: "off"`` comment produces no error to count and
 * no suppressed message either, so a file can switch a rule off with nothing
 * anywhere to show for it. ``global`` and ``exported`` are rejected on the same
 * ground: they change what rules see, per file, invisibly.
 *
 * The allow-list is node identity from ESLint's own directive parser rather than
 * a pattern match on comment text, so a directive spelling this file never
 * imagined is rejected by default instead of slipping through. Pure.
 */
export function inlineConfigViolations(sourceCode) {
  const suppressions = new Set(sourceCode.getDisableDirectives().directives.map((directive) => directive.node));
  return sourceCode
    .getInlineConfigNodes()
    .filter((node) => !suppressions.has(node))
    .map((node) => ({
      line: node.loc.start.line,
      column: node.loc.start.column + 1,
      text: `${node.value ?? ''}`.trim(),
    }));
}

/**
 * Run the parse-only pass over every intended file and collect what it rejects.
 *
 * A file the pass cannot produce a ``SourceCode`` for is reported rather than
 * skipped: unreadable is not the same as clean, and silently passing over it is
 * how the measurement would shrink again.
 */
export function forbiddenInlineConfig({ root, files, config, linter }) {
  const found = [];
  for (const file of files) {
    const code = fs.readFileSync(path.join(root, file), 'utf8');
    const messages = linter.verify(code, config, { filename: file, allowInlineConfig: false });
    const sourceCode = linter.getSourceCode();
    if (!sourceCode) {
      found.push({ file, line: 0, column: 0, text: messages[0]?.message ?? 'could not be parsed for inline directives' });
      continue;
    }
    for (const violation of inlineConfigViolations(sourceCode)) found.push({ file, ...violation });
  }
  return found;
}
