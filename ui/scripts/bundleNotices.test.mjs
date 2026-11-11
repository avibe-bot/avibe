import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { bundleNotices, classify, collectNotices, owningPackage } from './bundleNotices.mjs';

// The App's notices cover exactly the bundled modules: each maps to the installed
// package that owns it, and a bundled package without a notice file fails.
let repo;
const pkg = (path, manifest, files = { LICENSE: 'MIT License\n\nCopyright (c) Example\n' }) => {
  const root = join(repo, 'ui/node_modules', ...path.split('/'));
  mkdirSync(join(root, 'dist'), { recursive: true });
  writeFileSync(join(root, 'package.json'), JSON.stringify(manifest));
  for (const [name, text] of Object.entries(files)) writeFileSync(join(root, name), text);
  writeFileSync(join(root, 'dist/index.js'), '');
  return root;
};
const tools = (name) => ({ root: join(repo, 'ui/node_modules', name), name, version: '8.0.0', license: 'MIT' });

beforeEach(() => {
  repo = mkdtempSync(join(tmpdir(), 'bundle-notices-'));
  mkdirSync(join(repo, 'ui/src'), { recursive: true });
});
afterEach(() => rmSync(repo, { recursive: true, force: true }));

describe('bundled module ownership', () => {
  it('finds the installed package, scoped and nested, past manifests without a name', () => {
    const scoped = pkg('@radix-ui/react-slot', { name: '@radix-ui/react-slot', version: '1.5.0', license: 'MIT' });
    const nested = pkg('a/node_modules/b', { name: 'b', version: '2.0.0' });
    mkdirSync(join(scoped, 'dist/esm'), { recursive: true });
    writeFileSync(join(scoped, 'dist/esm/package.json'), JSON.stringify({ type: 'module' }));
    expect(owningPackage(join(scoped, 'dist/esm/index.mjs'))).toMatchObject({ name: '@radix-ui/react-slot', version: '1.5.0' });
    expect(owningPackage(join(nested, 'dist/index.js'))).toMatchObject({ name: 'b', version: '2.0.0' });
  });

  it('classifies local, package, and known virtual modules, and rejects the rest', () => {
    pkg('react', { name: 'react', version: '19.3.1', license: 'MIT' });
    const options = { repoRoot: repo };
    expect(classify(join(repo, 'ui/src/main.tsx?used'), options)).toEqual({ kind: 'local' });
    expect(classify(join(repo, 'ui/node_modules/react/dist/index.js?commonjs-es-import'), options)).toMatchObject({
      kind: 'package', owner: { name: 'react' },
    });
    expect(classify('\0vite/modulepreload-polyfill.js', options)).toEqual({ kind: 'virtual', owner: 'vite' });
    expect(classify('\0rolldown/runtime.js', options)).toEqual({ kind: 'virtual', owner: 'rolldown' });
    expect(() => classify('\0some-plugin/helper', options)).toThrow(/no known owner/);
    expect(() => classify('/elsewhere/file.js', options)).toThrow(/outside the repository/);
  });
});

describe('collectNotices', () => {
  it('keeps every notice file verbatim and counts modules per package version', () => {
    pkg('react', { name: 'react', version: '19.3.1', license: 'MIT' }, {
      LICENSE: 'MIT License\n\nCopyright (c) Meta Platforms, Inc. and affiliates.\n',
      'NOTICE.md': 'Third-party notice.\n',
      'license.js': 'not a notice',
    });
    const notices = collectNotices([
      join(repo, 'ui/node_modules/react/dist/index.js'),
      join(repo, 'ui/node_modules/react/dist/index.js'),
      join(repo, 'ui/src/main.tsx'),
      '\0vite/modulepreload-polyfill.js',
    ], { repoRoot: repo, resolvePackage: () => ({ ...tools('vite'), root: pkg('vite', { name: 'vite', version: '8.0.0' }) }) });
    expect(notices.packages).toEqual([
      { name: 'react', version: '19.3.1', license: 'MIT', modules: 1, files: [
        { name: 'LICENSE', text: 'MIT License\n\nCopyright (c) Meta Platforms, Inc. and affiliates.\n' },
        { name: 'NOTICE.md', text: 'Third-party notice.\n' },
      ] },
      { name: 'vite', version: '8.0.0', license: 'MIT', modules: 1, files: [
        { name: 'LICENSE', text: 'MIT License\n\nCopyright (c) Example\n' },
      ] },
    ]);
    expect(notices.local_modules).toBe(1);
    expect(notices.virtual_modules).toEqual({ 'vite/modulepreload-polyfill.js': 'vite@8.0.0' });
    expect(JSON.stringify(notices)).not.toContain(repo);
  });

  it('fails with the package and path when a bundled package ships no notice', () => {
    const root = pkg('bare', { name: 'bare', version: '1.0.0', license: 'MIT' }, {});
    expect(() => collectNotices([join(root, 'dist/index.js')], { repoRoot: repo, resolvePackage: tools }))
      .toThrow(new RegExp(`bare@1\\.0\\.0 \\(${root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\)`));
  });
});

describe('bundleNotices plugin', () => {
  it('adds the third-party files a stylesheet inlined, and only those', () => {
    const tailwind = pkg('tailwindcss', { name: 'tailwindcss', version: '4.0.0', license: 'MIT' });
    const notices = bundleNotices({ repoRoot: repo, uiRoot: join(repo, 'ui') });
    const css = join(repo, 'ui/src/index.css');
    notices.postcss.OnceExit(null, { result: { opts: { from: css }, messages: [
      { type: 'dependency', file: join(tailwind, 'index.css') },
      { type: 'dependency', file: join(repo, 'ui/src/App.tsx') },
      { type: 'dir-dependency', dir: join(repo, 'ui/src') },
    ] } });
    const emitted = [];
    notices.vite.generateBundle.call({ emitFile: (file) => emitted.push(file) }, {}, {
      'mobile.js': { type: 'chunk', moduleIds: [css] },
      'mobile.html': { type: 'asset', originalFileNames: ['mobile.html'] },
    });
    const data = JSON.parse(emitted[0].source);
    expect(emitted[0].fileName).toBe('licenses-web.json');
    expect(data.packages.map((p) => `${p.name}@${p.version}`)).toEqual(['tailwindcss@4.0.0']);
    expect(data.local_modules).toBe(2);
  });
});
