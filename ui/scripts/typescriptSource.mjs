// Shared syntax parsing for the theme guard and structural tests. TypeScript
// 7.0.2 has no standalone createSourceFile: its native compiler parses a project.
// A hermetic in-memory single-file project preserves the AST, parent pointers
// and UTF-16 offsets these consumers need without loading imports or disk files.
// Pin 7.0.2 while these published APIs are under unstable/*.
import { API } from 'typescript/unstable/sync';
import { SyntaxKind, createScanner } from 'typescript/unstable/ast';
import { createToken } from 'typescript/unstable/ast/factory';

const ROOT = '/__avibe_typescript_source__';
const CONFIG = `${ROOT}/tsconfig.json`;

let service = null;

function createService() {
  const files = new Map();
  let config = '';
  const fs = {
    // `null` is "does not exist" without a fallback to the real filesystem.
    readFile: (name) => (name === CONFIG ? config : files.get(name) ?? null),
    fileExists: (name) => name === CONFIG || files.has(name),
    directoryExists: (name) => name === ROOT || name === '/',
    getAccessibleEntries: () => ({ files: [], directories: [] }),
    realpath: (name) => name,
  };
  const api = new API({ cwd: ROOT, fs });
  let counter = 0;
  let previous = null;
  // TypeScript owns the child process and kills it on Node exit. Retain only
  // the latest snapshot here; returned syntax trees remain ordinary JS nodes.
  return {
    parse(text, fileName) {
      // TS5 callers chose ScriptKind TS for `.ts/.mts/.cts` and TSX otherwise; the
      // compiler derives the kind (and a declaration file) from the name, so the
      // virtual name keeps the suffix.
      const suffix = /(?:\.d)?\.[mc]?ts$/.exec(fileName)?.[0] ?? '.tsx';
      const path = `${ROOT}/source${counter++}${suffix}`;
      files.clear();
      files.set(path, text);
      config = JSON.stringify({ compilerOptions: { noLib: true, types: [], noEmit: true, jsx: 'preserve' }, files: [path] });
      const snapshot = previous === null
        ? api.updateSnapshot({ openProjects: [CONFIG] })
        : api.updateSnapshot({ fileChanges: { changed: [CONFIG], created: [path] } });
      const sourceFile = snapshot.getProject(CONFIG)?.program.getSourceFile(path);
      previous?.dispose();
      previous = snapshot;
      if (sourceFile === undefined) throw new Error(`TypeScript 7 did not parse ${fileName}`);
      return sourceFile;
    },
  };
}

/** One file's syntax tree, parented, in UTF-16 offsets of `text`. */
export function parseSource(text, fileName) {
  service ??= createService();
  return service.parse(text, fileName);
}

const isTokenNode = (node) => node.kind <= SyntaxKind.LastToken;

/**
 * TS5 `node.getChildren(sourceFile)` without its `SyntaxList` wrappers: the AST children
 * in source order, plus the punctuation and keyword tokens the AST does not store,
 * recovered by scanning the gaps between children -- the same technique TS7's own
 * `getTokenAtPosition`/`findNextToken` use. Recovered tokens are real token nodes
 * (`getStart`, `getText`, `parent`).
 */
export function childrenOf(node, sourceFile) {
  if (isTokenNode(node)) return [];
  const nodes = [];
  node.forEachChild((child) => { nodes.push(child); }, (list) => { nodes.push(...list); });
  nodes.sort((a, b) => a.pos - b.pos);
  const out = [];
  const scanner = createScanner(true, sourceFile.languageVariant, sourceFile.text);
  const recover = (from, to) => {
    if (from >= to) return;
    // Stop at `to` exactly, as astnav does: the scanner knows nothing about the AST,
    // and past the gap it would read the next child's bytes (`# {…}` in JSX text
    // scans as a zero-width PrivateIdentifier forever).
    scanner.resetTokenState(from);
    for (let left = from; left < to;) {
      const kind = scanner.scan();
      const end = scanner.getTokenEnd();
      if (kind === SyntaxKind.EndOfFile || end > to || end <= left) break;
      const token = createToken(kind);
      token.pos = scanner.getTokenFullStart();
      token.end = end;
      token.parent = node;
      out.push(token);
      left = end;
    }
  };
  let cursor = node.pos;
  for (const child of nodes) {
    recover(cursor, child.pos);
    out.push(child);
    cursor = child.end;
  }
  recover(cursor, node.end);
  return out;
}
