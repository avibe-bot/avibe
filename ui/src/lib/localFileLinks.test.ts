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
    expect(resolveLocalFileLink('./a%5Cb', '/workspace')).toEqual({
      path: '/workspace/a\\b',
    });
    expect(resolveLocalFileLink('./a%5Cb', 'C:\\workspace')).toEqual({
      path: 'C:\\workspace\\a\\b',
    });
  });

  it('extracts common source line and column suffixes', () => {
    expect(resolveLocalFileLink('/root/app.py:42')).toEqual({
      path: '/root/app.py',
      line: 42,
      column: 0,
      endColumn: 0,
    });
    expect(resolveLocalFileLink('./app.py:42:7', '/workspace')).toEqual({
      path: '/workspace/app.py',
      line: 42,
      column: 6,
      endColumn: 6,
    });
  });

  it('recognizes absolute Windows destinations without treating the drive as a URL scheme', () => {
    expect(resolveLocalFileLink('C:/workspace/app.py:42')).toEqual({
      path: 'C:/workspace/app.py',
      line: 42,
      column: 0,
      endColumn: 0,
    });
    expect(resolveLocalFileLink('C:%5Cworkspace%5Capp.py:42:7')).toEqual({
      path: 'C:\\workspace\\app.py',
      line: 42,
      column: 6,
      endColumn: 6,
    });
  });

  it('recognizes raw and encoded absolute UNC destinations', () => {
    expect(resolveLocalFileLink('\\\\server\\share\\app.py:42')).toEqual({
      path: '\\\\server\\share\\app.py',
      line: 42,
      column: 0,
      endColumn: 0,
    });
    expect(resolveLocalFileLink('%5C%5Cserver%5Cshare%5Capp.py:42:7')).toEqual({
      path: '\\\\server\\share\\app.py',
      line: 42,
      column: 6,
      endColumn: 6,
    });
  });

  it('decodes filenames only after identifying literal source suffixes', () => {
    expect(resolveLocalFileLink('/tmp/report%3A2026')).toEqual({
      path: '/tmp/report:2026',
    });
    expect(resolveLocalFileLink('/tmp/report%3A2026:42')).toEqual({
      path: '/tmp/report:2026',
      line: 42,
      column: 0,
      endColumn: 0,
    });
  });

  it('leaves web URLs and unresolved relative paths alone', () => {
    for (const href of ['https://example.com/file', 'c:relative-file', '//cdn.example.com/file', '../file.ts', 'file.ts', '/', './']) {
      expect(resolveLocalFileLink(href, '/workspace')).toBeNull();
    }
    expect(resolveLocalFileLink('./file.ts', null)).toBeNull();
    expect(resolveLocalFileLink('./file.ts', 'relative/workdir')).toBeNull();
  });

  it('leaves current application routes as same-origin browser links', () => {
    for (const href of [
      '/chat/session-123',
      '/apps/show/session-123',
      '/apps/files',
      '/apps/files/',
      '/admin/settings/backends/codex',
      '/settings/models?source=custom',
      '/doctor/logs#latest',
    ]) {
      expect(resolveLocalFileLink(href, '/workspace')).toBeNull();
    }
  });

  it('does not reserve application route namespaces as filesystem roots', () => {
    for (const path of [
      '/projects/report.md',
      '/apps/source.ts',
      '/chat/session-123/notes.md',
      '/admin/settings/custom.json',
    ]) {
      expect(resolveLocalFileLink(path, '/workspace')).toEqual({ path });
    }
  });
});
