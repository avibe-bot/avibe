// Independent enforcement contract for the Oxlint gate. The configuration on
// disk does not import this module: changing its scope, rules, options or plugin
// identity must fail before a debt ledger can be trusted or regenerated.
import fs from 'node:fs';
import path from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { EXPECTED_BROWSER_GLOBALS } from './browserGlobals.mjs';

export const DEPENDENCY_ROOTS = ['node_modules', 'dist'];
export const INTENDED_EXTENSIONS = ['.ts', '.tsx'];
export const CONFIG_FILE = '.oxlintrc.json';
export const OXLINT_VERSION = '1.92.0';

// Keep the ledger's established rule identities while replacing their engine.
// A rename is not a debt reduction. Native aliases and JS-plugin aliases are
// mapped explicitly below, never by dropping an unrecognized diagnostic.
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
  '@typescript-eslint/no-unused-vars': [2, { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_', destructuredArrayIgnorePattern: '^_' }],
  '@typescript-eslint/no-wrapper-object-types': [2],
  '@typescript-eslint/prefer-as-const': [2],
  '@typescript-eslint/prefer-namespace-keyword': [2],
  '@typescript-eslint/triple-slash-reference': [2],
  'avibe/localized-user-text': [2],
  'for-direction': [2],
  'no-async-promise-executor': [2],
  'no-case-declarations': [2],
  'no-compare-neg-zero': [2],
  'no-cond-assign': [2, 'except-parens'],
  'no-constant-binary-expression': [2, { checkRelationalComparisons: false }],
  'no-constant-condition': [2, { checkLoops: 'allExceptWhileTrue' }],
  'no-control-regex': [2],
  'no-debugger': [2],
  'no-delete-var': [2],
  'no-dupe-else-if': [2],
  'no-duplicate-case': [2],
  'no-empty': [2, { allowEmptyCatch: false }],
  'no-empty-character-class': [2],
  'no-empty-pattern': [2, { allowObjectPatternsAsParameters: false }],
  'no-empty-static-block': [2],
  'no-ex-assign': [2],
  'no-extra-boolean-cast': [2, {}],
  'no-fallthrough': [2, { allowEmptyCase: false, reportUnusedFallthroughComment: false }],
  'no-global-assign': [2, { exceptions: [] }],
  'no-invalid-regexp': [2, {}],
  'no-irregular-whitespace': [2, { skipComments: false, skipJSXText: false, skipRegExps: false, skipStrings: true, skipTemplates: false }],
  'no-loss-of-precision': [2],
  'no-misleading-character-class': [2, { allowEscape: false }],
  'no-nonoctal-decimal-escape': [2],
  'no-octal': [2],
  'no-prototype-builtins': [2],
  'no-regex-spaces': [2],
  'no-self-assign': [2, { props: true }],
  'no-shadow-restricted-names': [2, { reportGlobalThis: true }],
  'no-sparse-arrays': [2],
  'no-unassigned-vars': [2],
  'no-unexpected-multiline': [2],
  'no-unsafe-finally': [2],
  'no-unsafe-optional-chaining': [2, { disallowArithmeticOperators: false }],
  'no-unused-labels': [2],
  'no-unused-private-class-members': [2],
  'no-useless-assignment': [2],
  'no-useless-backreference': [2],
  'no-useless-catch': [2],
  'no-useless-escape': [2, { allowRegexCharacters: [] }],
  'no-var': [2],
  'prefer-const': [2, { destructuring: 'any', ignoreReadBeforeAssign: false }],
  'prefer-rest-params': [2],
  'prefer-spread': [2],
  'preserve-caught-error': [2, { requireCatchParameter: false, errorClassNames: [] }],
  'react-hooks/config': [2],
  'react-hooks/error-boundaries': [2],
  'react-hooks/exhaustive-deps': [1],
  'react-hooks/gating': [2],
  'react-hooks/globals': [2],
  'react-hooks/immutability': [2],
  'react-hooks/incompatible-library': [1],
  'react-hooks/preserve-manual-memoization': [2],
  'react-hooks/purity': [2],
  'react-hooks/refs': [2],
  'react-hooks/rules-of-hooks': [2],
  'react-hooks/set-state-in-effect': [2],
  'react-hooks/set-state-in-render': [2],
  'react-hooks/static-components': [2],
  'react-hooks/unsupported-syntax': [1],
  'react-hooks/use-memo': [2],
  'react-refresh/only-export-components': [2, { allowConstantExport: true }],
  'require-yield': [2],
  'use-isnan': [2, { enforceForIndexOf: false, enforceForSwitchCase: true }],
  'valid-typeof': [2, { requireStringLiterals: false }],
};

// Oxlint's core implementations already understand TypeScript for these three
// rules; installing the old TS plugin to extend them would be counterproductive.
const NATIVE_EXTENSIONS = new Set(['no-array-constructor', 'no-unused-expressions', 'no-unused-vars']);
export function oxlintRuleId(id) {
  // Oxlint's assignment analysis currently misses writes across try/finally.
  // Reuse the upstream rule until that demonstrated coverage gap is closed.
  if (id === 'no-octal' || id === 'no-useless-assignment') return `avibe/${id}`;
  if (id.startsWith('@typescript-eslint/')) {
    const name = id.slice('@typescript-eslint/'.length);
    return `${NATIVE_EXTENSIONS.has(name) ? 'eslint' : 'typescript'}/${name}`;
  }
  if (id.startsWith('react-hooks/')) return id.replace('react-hooks/', 'react-hooks-js/');
  return id.includes('/') ? id : `eslint/${id}`;
}
const LEDGER_IDS = new Map(Object.keys(EXPECTED_RULES).map((id) => [oxlintRuleId(id), id]));
export const ledgerRuleId = (id) => LEDGER_IDS.get(id) ?? id;

export const EXPECTED_CONFIG = {
  plugins: ['eslint', 'typescript'],
  categories: { correctness: 'off', nursery: 'off', pedantic: 'off', perf: 'off', restriction: 'off', style: 'off', suspicious: 'off' },
  jsPlugins: [
    { name: 'react-hooks-js', specifier: 'eslint-plugin-react-hooks' },
    'eslint-plugin-react-refresh',
    { name: 'avibe', specifier: './scripts/lintPlugin.mjs' },
  ],
  env: { builtin: false, es2020: true },
  globals: Object.fromEntries(Object.entries(EXPECTED_BROWSER_GLOBALS).map(([name, writable]) => [name, writable ? 'writable' : 'readonly'])),
  options: { reportUnusedDisableDirectives: 'warn', respectEslintDisableDirectives: true },
  ignorePatterns: ['**/node_modules/**', '**/dist/**'],
  overrides: [{
    files: ['**/*.ts', '**/*.tsx'],
    rules: Object.fromEntries(Object.entries(EXPECTED_RULES).map(([id, options]) => [
      oxlintRuleId(id),
      // The empty extra-class list has no behavior to migrate. Oxlint accepts
      // requireCatchParameter but rejects that unsupported empty option.
      id === 'preserve-caught-error' ? [2, { requireCatchParameter: false }] : options,
    ])),
  }],
};

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

// Compare the complete input configuration, not a lossy --print-config result
// (Oxlint 1.91 omits JS-plugin rule entries there). The gate additionally fixes
// the CLI flags and disables nested configs, so there is no other policy layer.
export function policyDifferences(actual, expected = EXPECTED_CONFIG) {
  if (!actual || typeof actual !== 'object') return ['no Oxlint configuration was loaded'];
  const lines = [];
  const visit = (value, wanted, key) => {
    if (isDeepStrictEqual(value, wanted)) return;
    if (value && wanted && typeof value === 'object' && typeof wanted === 'object'
      && Array.isArray(value) === Array.isArray(wanted)) {
      for (const child of new Set([...Object.keys(wanted), ...Object.keys(value)])) {
        visit(value[child], wanted[child], key ? `${key}.${child}` : child);
      }
    } else {
      lines.push(`${key}: expected ${JSON.stringify(wanted)}, resolved ${JSON.stringify(value)}`);
    }
  };
  visit(actual, expected, '');
  return lines;
}

export function coverageGaps({ intended, lintedFiles }) {
  const walked = new Set(intended);
  const linted = new Set(lintedFiles);
  return {
    empty: intended.length === 0,
    missing: intended.filter((file) => !linted.has(file)),
    unwalked: [...linted].filter((file) => INTENDED_EXTENSIONS.some((extension) => file.endsWith(extension)))
      .filter((file) => !walked.has(file)).sort(),
  };
}

// Input is actual parsed comments, never a scan of string/template contents.
// Oxlint recognizes disable/enable comments only; inline globals/exported and
// rule reconfiguration are still rejected rather than silently changing policy.
export function inlineConfigViolations(comments, code) {
  const result = [];
  for (const comment of comments) {
    const text = comment.value.trim();
    if (!/^(?:eslint|oxlint|globals?|exported)\b/u.test(text)) continue;
    if (/^(?:eslint|oxlint)-(?:disable(?:-next-line|-line)?|enable)(?:\s|$)/u.test(text)) continue;
    // globals/exported and rule configuration are block-comment directives;
    // ordinary prose line comments starting with "global" are not config.
    if (comment.type !== 'Block') continue;
    const prefix = code.slice(0, comment.start);
    result.push({ line: prefix.split('\n').length, column: prefix.length - prefix.lastIndexOf('\n'), text });
  }
  return result;
}
