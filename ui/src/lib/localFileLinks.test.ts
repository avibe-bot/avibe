import { describe, expect, it } from 'vitest';

import { resolveLocalFileLink } from './localFileLinks';

describe('resolveLocalFileLink', () => {
  it('keeps absolute POSIX paths and decodes Markdown URL escapes', () => {
    expect(resolveLocalFileLink('/root/My%20Report.md')).toEqual({ path: '/root/My Report.md' });
  });

  it('resolves ./ paths against the Agent Session workdir', () => {
    expect(resolveLocalFileLink('./src/main.ts', '/workspace/project')).toEqual({
      path: '/workspace/project/src/main.ts',
    });
    expect(resolveLocalFileLink('./src/main.ts', '/')).toEqual({ path: '/src/main.ts' });
    expect(resolveLocalFileLink('./src/main.ts', 'C:\\workspace\\project\\')).toEqual({
      path: 'C:\\workspace\\project\\src\\main.ts',
    });
  });

  it('extracts common source line and column suffixes', () => {
    expect(resolveLocalFileLink('/root/app.py:42')).toEqual({
      path: '/root/app.py',
      line: 42,
      column: 1,
      endColumn: 1,
    });
    expect(resolveLocalFileLink('./app.py:42:7', '/workspace')).toEqual({
      path: '/workspace/app.py',
      line: 42,
      column: 7,
      endColumn: 7,
    });
  });

  it('leaves web URLs and unresolved relative paths alone', () => {
    for (const href of ['https://example.com/file', '//cdn.example.com/file', '../file.ts', 'file.ts', '/', './']) {
      expect(resolveLocalFileLink(href, '/workspace')).toBeNull();
    }
    expect(resolveLocalFileLink('./file.ts', null)).toBeNull();
    expect(resolveLocalFileLink('./file.ts', 'relative/workdir')).toBeNull();
  });
});
