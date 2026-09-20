/* @vitest-environment jsdom */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { build, createServer } from 'vite';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const entry = '\0avibe-sanitizer-probe';
let probe;
let modules;

beforeAll(async () => {
  vi.stubGlobal('matchMedia', () => ({
    matches: false, addEventListener() {}, removeEventListener() {},
  }));
  // Bundle the actual Monaco consumers using the app's Vite config. Importing
  // the npm sanitizer alone would pass even if Monaco still used its copy.
  const result = await build({
    root,
    configFile: path.join(root, 'vite.config.ts'),
    logLevel: 'silent',
    plugins: [{
      name: 'sanitizer-test-entry',
      resolveId: id => id === entry ? entry : undefined,
      load: id => id === entry ? `
        export { default as purify } from 'dompurify';
        export { safeSetInnerHtml } from 'monaco-editor/esm/vs/base/browser/domSanitize.js';
        export { renderMarkdown } from 'monaco-editor/esm/vs/base/browser/markdownRenderer.js';
      ` : undefined,
    }],
    build: {
      write: false, minify: false,
      rollupOptions: {
        input: entry, preserveEntrySignatures: 'strict',
        output: { format: 'iife', name: 'SanitizerProbe' },
      },
    },
  });
  const chunk = result.output.find(item => item.type === 'chunk' && item.isEntry);
  modules = Object.keys(chunk.modules).map(id => id.replaceAll('\\', '/'));
  // Only generated, trusted library code executes; event payloads below are
  // parsed inertly by JSDOM, never evaluated or inserted into a live browser.
  probe = new Function(`${chunk.code}\nreturn SanitizerProbe;`)();
}, 30_000);

afterAll(() => vi.unstubAllGlobals());

describe('Monaco consumes the patched sanitizer', () => {
  it('uses the same patched module through the development resolver', async () => {
    const server = await createServer({
      root, configFile: path.join(root, 'vite.config.ts'), logLevel: 'silent',
      server: { middlewareMode: true, watch: null, hmr: false },
      optimizeDeps: { noDiscovery: true, include: [] },
    });
    try {
      const resolved = await server.environments.client.pluginContainer.resolveId(
        './dompurify/dompurify.js',
        path.join(root, 'node_modules/monaco-editor/esm/vs/base/browser/domSanitize.js'),
      );
      expect(resolved.id.split('?')[0].replaceAll('\\', '/')).toBe(
        path.join(root, 'node_modules/dompurify/dist/purify.es.mjs').replaceAll('\\', '/'),
      );
    } finally {
      await server.close();
    }
  });

  it('bundles the declared DOMPurify and excludes the copied vendor implementation', () => {
    expect(probe.purify.version).toBe(manifest.dependencies.dompurify);
    expect(modules.some(id => id.endsWith('/dompurify/dist/purify.es.mjs'))).toBe(true);
    expect(modules.some(id => id.includes('/monaco-editor/') && id.endsWith('/dompurify.js'))).toBe(false);
    const sanitize = vi.spyOn(probe.purify, 'sanitize');
    try {
      probe.safeSetInnerHtml(document.createElement('div'), '<strong>normal</strong>');
      expect(sanitize).toHaveBeenCalledOnce();
    } finally {
      sanitize.mockRestore();
    }
  });

  it.each(['xmp', 'iframe', 'noembed', 'noframes'])('blocks the old %s recontextualization regression', wrapper => {
    const input = `<img src=x alt="</${wrapper}><img src=x onerror=unsafe()>">`;
    const sink = document.createElement('div');
    sink.innerHTML = `<${wrapper}>${probe.purify.sanitize(input)}</${wrapper}>`;
    expect(sink.querySelector('[onerror]')).toBeNull();
  });

  it.each([
    '<img src=x onerror="globalThis.__unsafe=1">',
    '<a href="javascript:globalThis.__unsafe=1">bad</a>',
    '<svg onload="globalThis.__unsafe=1"></svg>',
    '<template><img src=x onerror="globalThis.__unsafe=1"></template>',
    '<img src=x alt="</xmp><img src=x onerror=globalThis.__unsafe=1>">',
    '[bad](javascript:globalThis.__unsafe=1)',
  ])('removes executable markup through the actual consumers: %s', input => {
    const element = document.createElement('div');
    probe.safeSetInnerHtml(element, input);
    const markdown = probe.renderMarkdown({ value: input, isTrusted: false, supportHtml: false });
    try {
      for (const sink of [element, markdown.element]) {
        for (const node of sink.querySelectorAll('*')) {
          for (const attr of node.attributes) {
            expect(attr.name).not.toMatch(/^on/i);
            if (['href', 'src', 'data-href'].includes(attr.name)) {
              expect(attr.value).not.toMatch(/^\s*javascript:/i);
            }
          }
        }
      }
    } finally {
      markdown.dispose();
    }
  });

  it('preserves ordinary multilingual Markdown and HTTPS links', () => {
    const rendered = probe.renderMarkdown({ value: '**说明 café** [docs](https://example.invalid/guide)' });
    try {
      expect(rendered.element.querySelector('strong')?.textContent).toBe('说明 café');
      expect(rendered.element.querySelector('a')?.getAttribute('data-href')).toBe('https://example.invalid/guide');
    } finally {
      rendered.dispose();
    }
  });
});
