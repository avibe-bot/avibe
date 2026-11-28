// Guard against shipped surfaces continuing to render proposal kit copies (avibe-bot/avibe#3445):
//
// 1. Every file under `ui/design/kit/` must be reachable through static imports from at least one
//    `ui/design/frames/**/*.design.tsx` file, so retired kit copies are deleted when a surface ships.
// 2. When production code under `ui/src/` cites `ui/design: <frame-id>` (per `ui/design/AGENTS.md`),
//    the matching `.design.tsx` file must not still carry `status: 'approved'` backed by a runtime
//    `ui/design/kit/` proposal copy, unless listed in `MIXED_OR_NATIVE_APPROVED_KIT_FRAMES`.
// 3. No `shipped`-only `.design.tsx` file may import runtime kit component copies (excluding pure
//    shared data/caption helpers and an explicit allowlist of stage/native-shell wrappers).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseSync } from 'oxc-parser';
import { describe, expect, it } from 'vitest';

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));
const DESIGN_ROOT = path.join(UI_ROOT, 'design');
const FRAMES_ROOT = path.join(DESIGN_ROOT, 'frames');
const KIT_ROOT = path.join(DESIGN_ROOT, 'kit');
const SRC_ROOT = path.join(UI_ROOT, 'src');

/** Pure fixture-data or caption helpers under `ui/design/kit/` (not UI component copies). */
const SHARED_KIT_DATA_OR_CAPTION_HELPERS = new Set([
  'kit/draft.ts',
  'kit/vault-mnemonic/data.ts',
]);

/**
 * Design files cited by shipped `ui/src/` code that intentionally combine shipped web frames with
 * native-only or not-yet-shipped `approved` stand-ins from `ui/design/kit/`.
 * Keep this list minimal: remove an entry as soon as its remaining `approved` kit frames ship.
 */
const MIXED_OR_NATIVE_APPROVED_KIT_FRAMES = new Map([
  [
    'mobile-app/switcher',
    'Web pages are shipped; the 4 approved frames draw iOS-native sheet gestures and RuntimeFailed from kit/mobile-instance-sheet.tsx.',
  ],
  [
    'show-pages/desktop-native-controls',
    'AppKit native title bar stand-in (kit/desktop-native-show-controls.tsx) has no web component; popovers and Show below are shipped.',
  ],
  [
    'vault/mnemonic/create',
    'Standard frames are shipped; only the two future protected-sandbox handoff frames remain approved in kit/vault-mnemonic/create.tsx.',
  ],
  [
    'update/panel',
    'Draws the shipped update panel inside update/surfaces.tsx captions via kit/draft.ts with approved status (pre-existing outside #3445).',
  ],
]);

/**
 * Shipped-only design files that intentionally wrap shipped components in a stage or native-shell
 * wrapper from `ui/design/kit/` (never a proposal copy of the shipped component itself).
 */
const SHIPPED_WRAPPER_ALLOWLIST = new Map([
  ['desktop/upgrade-retry', 'Wraps shipped views in DesktopBackHeader native-shell stand-in.'],
  ['mobile-app/direct-tag', 'Uses SheetLanguage lang wrapper from kit/mobile-instance-sheet.tsx.'],
  ['mobile-app/status-dots', 'Uses SheetLanguage lang wrapper from kit/mobile-instance-sheet.tsx.'],
  ['motion/inbox', 'Uses motion/Stage.tsx presentation stage around shipped Inbox components.'],
  ['motion/navigation', 'Uses motion/Stage.tsx presentation stage around shipped navigation layers.'],
  ['motion/sheets', 'Uses motion/Stage.tsx presentation stage around shipped sheet layers.'],
]);

function walkTsFiles(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return walkTsFiles(full);
    return /\.tsx?$/.test(entry.name) ? [full] : [];
  });
}

function toPosixRelative(root, file) {
  return path.relative(root, file).split(path.sep).join('/');
}

function resolveRelativeModule(fromFile, specifier) {
  if (!specifier.startsWith('.')) return null;
  const base = path.resolve(path.dirname(fromFile), specifier);
  const candidates = [
    base,
    `${base}.ts`,
    `${base}.tsx`,
    path.join(base, 'index.ts'),
    path.join(base, 'index.tsx'),
  ];
  return candidates.find((candidate) => fs.existsSync(candidate) && fs.statSync(candidate).isFile()) ?? null;
}

/**
 * Extract static import/re-export module specifiers from a TS/TSX file via oxc-parser.
 * When `runtimeOnly` is true, skips type-only imports (`import type ...` or all-type specifiers).
 */
function staticSpecifiers(
  filePath,
  { runtimeOnly = false, ignoreNamed = new Set() } = {},
  source = fs.readFileSync(filePath, 'utf8'),
) {
  const { program } = parseSync(filePath, source);
  const specifiers = [];
  for (const node of program.body) {
    if (node.type === 'ImportDeclaration' && node.source?.value) {
      if (runtimeOnly) {
        if (node.importKind === 'type') continue;
        const valueSpecs = (node.specifiers ?? []).filter((spec) => spec.importKind !== 'type');
        if (node.specifiers?.length && valueSpecs.length === 0) continue;
        if (
          ignoreNamed.size > 0
          && valueSpecs.length > 0
          && valueSpecs.every((spec) => spec.type === 'ImportSpecifier' && ignoreNamed.has(spec.imported?.name))
        ) {
          continue;
        }
      }
      specifiers.push(node.source.value);
    } else if (
      (node.type === 'ExportNamedDeclaration' || node.type === 'ExportAllDeclaration')
      && node.source?.value
    ) {
      if (runtimeOnly && node.exportKind === 'type') continue;
      specifiers.push(node.source.value);
    }
  }
  return specifiers;
}

/**
 * Extract `ui/design:` frame references from comments in production source text.
 * Recognizes `ui/design: area/name`, `area/name/state`, `area/name/*`, and comma/semicolon lists.
 */
export function extractDesignRefs(source) {
  const refs = new Set();
  for (const match of source.matchAll(/ui\/design:\s*([^\n]+)/gi)) {
    const line = match[1].replace(/\*\/.*$/, '');
    for (const raw of line.split(/[,;]/)) {
      const cleaned = raw
        .trim()
        .replace(/^`+|`+$/g, '')
        .replace(/\s+.*$/, '')
        .replace(/[.):}]+$/, '')
        .replace(/\/\*$/, '')
        .replace(/\/\{[^}]*\}$/, '');
      if (cleaned && /^[a-z0-9-]+(?:\/[a-z0-9_*-]+)+$/i.test(cleaned)) {
        refs.add(cleaned);
      }
    }
  }
  return [...refs];
}

/** Whether a frame-file id (`mobile-app/welcome`) is referenced by any `ui/design:` id/prefix. */
export function matchesDesignRef(frameFileId, ref) {
  if (ref === frameFileId) return true;
  if (ref.startsWith(`${frameFileId}/`)) return true;
  return false;
}

function collectStatusLiterals(node, statuses) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'Literal' && (node.value === 'proposed' || node.value === 'approved' || node.value === 'shipped')) {
    statuses.add(node.value);
    return;
  }
  if (node.type === 'TSAsExpression' || node.type === 'TSSatisfiesExpression' || node.type === 'ParenthesizedExpression') {
    collectStatusLiterals(node.expression, statuses);
  } else if (node.type === 'LogicalExpression') {
    collectStatusLiterals(node.left, statuses);
    collectStatusLiterals(node.right, statuses);
  } else if (node.type === 'ConditionalExpression') {
    collectStatusLiterals(node.consequent, statuses);
    collectStatusLiterals(node.alternate, statuses);
  }
}

/** Collect all frame status literals (`proposed`, `approved`, `shipped`) declared on `status:` properties. */
export function declaredFrameStatuses(source, fileName = 'frame.tsx') {
  const { program } = parseSync(fileName, source);
  const statuses = new Set();
  const visit = (node) => {
    if (!node || typeof node !== 'object') return;
    if (
      node.type === 'Property'
      && ((node.key?.type === 'Identifier' && node.key.name === 'status')
        || (node.key?.type === 'Literal' && node.key.value === 'status'))
    ) {
      collectStatusLiterals(node.value, statuses);
      return;
    }
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) {
        for (const item of value) visit(item);
      } else if (value && typeof value === 'object' && 'type' in value) {
        visit(value);
      }
    }
  };
  visit(program);
  return statuses;
}

/**
 * Collect the files reachable from `entryFile` within `DESIGN_ROOT` (split into local helpers and
 * `kit/` files).
 */
function collectDesignDependencies(entryFile, { runtimeOnly = false, ignoreNamed = new Set() } = {}) {
  const seen = new Set();
  const kitFiles = new Set();
  const helperFiles = new Set();
  const queue = [entryFile];
  while (queue.length) {
    const current = queue.pop();
    if (seen.has(current)) continue;
    seen.add(current);
    for (const specifier of staticSpecifiers(current, { runtimeOnly, ignoreNamed })) {
      const resolved = resolveRelativeModule(current, specifier);
      if (!resolved || !resolved.startsWith(DESIGN_ROOT + path.sep)) continue;
      if (resolved.startsWith(KIT_ROOT + path.sep)) {
        kitFiles.add(resolved);
        queue.push(resolved);
      } else if (resolved.startsWith(FRAMES_ROOT + path.sep) && !resolved.endsWith('.design.tsx')) {
        helperFiles.add(resolved);
        queue.push(resolved);
      }
    }
  }
  return { kitFiles, helperFiles };
}

/**
 * Find shipped-referenced `.design.tsx` files that still declare `approved` status and depend on a
 * runtime `ui/design/kit/` module.
 */
export function findApprovedKitViolations(entries, shippedRefs, allowlist = new Map()) {
  const violations = [];
  const matchedAllowlist = new Set();
  for (const entry of entries) {
    if (!entry.statuses.has('approved') || entry.kitFiles.length === 0) continue;
    const matchedRefs = shippedRefs.filter((ref) => matchesDesignRef(entry.frameFileId, ref));
    if (matchedRefs.length === 0) continue;
    if (allowlist.has(entry.frameFileId)) {
      matchedAllowlist.add(entry.frameFileId);
      continue;
    }
    violations.push({
      frameFileId: entry.frameFileId,
      kitFiles: entry.kitFiles,
      refs: matchedRefs,
    });
  }
  const staleAllowlist = [...allowlist.keys()].filter((id) => !matchedAllowlist.has(id));
  return { violations, staleAllowlist };
}

/**
 * Find `shipped`-only `.design.tsx` files that import runtime kit component copies (ignoring pure
 * data/caption helpers and explicit wrapper allowlist entries).
 */
export function findShippedKitViolations(entries, allowlist = new Map()) {
  const violations = [];
  const matchedAllowlist = new Set();
  for (const entry of entries) {
    const shippedOnly = entry.statuses.has('shipped')
      && !entry.statuses.has('approved')
      && !entry.statuses.has('proposed');
    if (!shippedOnly) continue;
    const componentKits = entry.kitFiles.filter((file) => !SHARED_KIT_DATA_OR_CAPTION_HELPERS.has(file));
    if (componentKits.length === 0) continue;
    if (allowlist.has(entry.frameFileId)) {
      matchedAllowlist.add(entry.frameFileId);
      continue;
    }
    violations.push({
      frameFileId: entry.frameFileId,
      kitFiles: componentKits,
    });
  }
  const staleAllowlist = [...allowlist.keys()].filter((id) => !matchedAllowlist.has(id));
  return { violations, staleAllowlist };
}

function scanFrameEntries() {
  const frameFiles = walkTsFiles(FRAMES_ROOT).filter((file) => file.endsWith('.design.tsx'));
  // `copyFor` is a shared i18n utility exported from `desktop/instances-fixtures.ts` alongside
  // `proposed` desktop frames; importing only `copyFor` does not inherit those frame statuses.
  const pureCopyUtilities = new Set(['copyFor']);
  return frameFiles.map((frameFile) => {
    const frameFileId = toPosixRelative(FRAMES_ROOT, frameFile).replace(/\.design\.tsx$/, '');
    const frameDir = path.dirname(frameFile);
    const { kitFiles, helperFiles } = collectDesignDependencies(frameFile, {
      runtimeOnly: true,
      ignoreNamed: pureCopyUtilities,
    });
    const statusSources = [
      frameFile,
      ...[...helperFiles].filter((helper) => path.dirname(helper) === frameDir),
    ];
    const statuses = new Set();
    for (const file of statusSources) {
      for (const status of declaredFrameStatuses(fs.readFileSync(file, 'utf8'), file)) {
        statuses.add(status);
      }
    }
    return {
      frameFileId,
      statuses,
      kitFiles: [...kitFiles].map((file) => toPosixRelative(DESIGN_ROOT, file)).sort(),
    };
  });
}

describe('design kit and shipped-frame guard (#3445)', () => {
  it('flags approved kit-backed frames once shipped code cites them, and flags shipped frames importing kit copies', () => {
    const entries = [
      { frameFileId: 'mobile-app/welcome', statuses: new Set(['approved']), kitFiles: ['kit/mobile-app.tsx'] },
      { frameFileId: 'mobile-app/account', statuses: new Set(['shipped']), kitFiles: ['kit/mobile-app.tsx'] },
      { frameFileId: 'mobile-app/empty', statuses: new Set(['shipped']), kitFiles: [] },
      { frameFileId: 'future/draft', statuses: new Set(['approved']), kitFiles: ['kit/future.tsx'] },
    ];
    const refs = extractDesignRefs(
      '// ui/design: mobile-app/welcome, mobile-app/empty/default\n// ui/design: mobile-app/welcome/providers-row',
    );
    expect(findApprovedKitViolations(entries, refs)).toEqual({
      violations: [
        {
          frameFileId: 'mobile-app/welcome',
          kitFiles: ['kit/mobile-app.tsx'],
          refs: ['mobile-app/welcome', 'mobile-app/welcome/providers-row'],
        },
      ],
      staleAllowlist: [],
    });
    expect(findShippedKitViolations(entries)).toEqual({
      violations: [
        {
          frameFileId: 'mobile-app/account',
          kitFiles: ['kit/mobile-app.tsx'],
        },
      ],
      staleAllowlist: [],
    });
  });

  it('has no orphaned files under ui/design/kit/', () => {
    const allKitFiles = walkTsFiles(KIT_ROOT).sort();
    const frameFiles = walkTsFiles(FRAMES_ROOT).filter((file) => file.endsWith('.design.tsx'));
    const reachableKitFiles = new Set();
    for (const frameFile of frameFiles) {
      const { kitFiles } = collectDesignDependencies(frameFile, { runtimeOnly: false });
      for (const kitFile of kitFiles) reachableKitFiles.add(kitFile);
    }
    const orphaned = allKitFiles
      .filter((kitFile) => !reachableKitFiles.has(kitFile))
      .map((kitFile) => toPosixRelative(DESIGN_ROOT, kitFile));
    expect(orphaned).toEqual([]);
  });

  it('has no shipped ui/src surface still drawn by an approved kit copy, and no shipped-only frame importing a kit copy', () => {
    const prodSrcFiles = walkTsFiles(SRC_ROOT).filter((file) => !/\.test\.tsx?$/.test(file));
    const shippedRefs = [...new Set(prodSrcFiles.flatMap((file) => extractDesignRefs(fs.readFileSync(file, 'utf8'))))];
    const entries = scanFrameEntries();

    expect(findApprovedKitViolations(entries, shippedRefs, MIXED_OR_NATIVE_APPROVED_KIT_FRAMES)).toEqual({
      violations: [],
      staleAllowlist: [],
    });
    expect(findShippedKitViolations(entries, SHIPPED_WRAPPER_ALLOWLIST)).toEqual({
      violations: [],
      staleAllowlist: [],
    });
  });
});
