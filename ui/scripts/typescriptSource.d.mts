import type { Node, SourceFile } from 'typescript/unstable/ast';

/** One file's syntax tree, parented, in UTF-16 offsets of `text`; the extension picks TS or TSX. */
export function parseSource(text: string, fileName: string): SourceFile;

/** TS5 `getChildren` without `SyntaxList`: AST children plus the tokens the AST does not store. */
export function childrenOf(node: Node, sourceFile: SourceFile): Node[];
