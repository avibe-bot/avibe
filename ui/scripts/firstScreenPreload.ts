// What the first screen needs beyond the entry starts downloading with it
// (docs/plans/web-ui-first-load.md). The reader's locale is its own chunk
// (src/i18n/app.ts) and a conversation's page is a lazy route
// (src/routePages.ts); without this, each would be requested only once the
// entry has downloaded and run, one round trip after it.
//
// index.html gets one small inline script, placed before anything that could
// hold it back (a stylesheet blocks a later inline script), that adds a
// `modulepreload` for each file the page will ask for:
//
// - the locale chunk, by the language detector's own order: the stored pick,
//   then the browser's first language (src/i18n/options.ts);
// - a conversation's page and its composer's editor, when the page opened on
//   is a conversation, or when an installed iPhone app launching at `/` will
//   restore one (src/lib/pwaRouteMemory.ts keeps the page it restores).
//
// A wrong guess only costs that download later, or one that goes unused.
import type { Plugin, Rolldown } from 'vite';

const LOCALES = { en: '/src/i18n/en.json', zh: '/src/i18n/zh.json' } as const;
const CHAT_MODULES = ['/src/components/workbench/ChatPage.tsx', '/src/components/workbench/MentionEditor.tsx'];
// The key src/lib/pwaRouteMemory.ts stores the restorable page under.
const PWA_LAST_ROUTE_KEY = 'avibe.pwa.last-route.v1';

export function firstScreenPreload(): Plugin {
  return {
    name: 'avibe-first-screen-preload',
    apply: 'build',
    transformIndexHtml: {
      order: 'post',
      handler(html, ctx) {
        const { bundle, chunk: entry } = ctx;
        // Only the Web UI's page: other pages built with this config (the design
        // canvas, design.html) bundle both locales and need nothing preloaded.
        if (!bundle || !entry || ctx.path !== '/index.html') return html;
        const chunks = Object.values(bundle).filter(
          (output): output is Rolldown.OutputChunk => output.type === 'chunk',
        );
        const byFile = new Map(chunks.map((chunk) => [chunk.fileName, chunk]));
        const closure = (roots: string[]) => {
          const seen = new Set<string>();
          const pending = [...roots];
          while (pending.length) {
            const file = pending.pop()!;
            if (seen.has(file)) continue;
            seen.add(file);
            pending.push(...(byFile.get(file)?.imports ?? []));
          }
          return seen;
        };
        // index.html already loads the entry and everything it imports.
        const loaded = closure([entry.fileName]);
        const files = (modules: readonly string[]) => {
          const roots = modules.map((id) => {
            const chunk = chunks.find((candidate) => candidate.facadeModuleId?.endsWith(id));
            if (!chunk) throw new Error(`avibe-first-screen-preload: no chunk for ${id}`);
            return chunk.fileName;
          });
          return [...closure(roots)].filter((file) => !loaded.has(file)).map((file) => `/${file}`);
        };
        // `L` (the locale files) and `C` (a conversation's) are also what
        // scripts/check-first-load-budget.mjs reads the first screen from.
        const locales = JSON.stringify({ en: files([LOCALES.en]), zh: files([LOCALES.zh]) });
        const chat = JSON.stringify(files(CHAT_MODULES));
        const script = `(function(){var L=${locales},C=${chat},d=document,p=location.pathname,l='';`
          + `try{l=localStorage.getItem('i18nextLng')||'';`
          + `if(p==='/'&&navigator.standalone)p=localStorage.getItem(${JSON.stringify(PWA_LAST_ROUTE_KEY)})||p}catch(e){}`
          + `if(!l){var n=navigator.languages;l=(n&&n[0])||navigator.language||''}`
          + `var f=/^zh/i.test(l)?L.zh:L.en;if(/^\\/chat\\/[^/]+$/.test(p))f=f.concat(C);`
          + `f.forEach(function(h){var k=d.createElement('link');k.rel='modulepreload';k.crossOrigin='';k.href=h;d.head.appendChild(k)})})()`;
        const charset = /<meta charset="[^"]*"\s*\/?>/i;
        if (!charset.test(html)) throw new Error('avibe-first-screen-preload: index.html has no <meta charset>');
        return html.replace(charset, (tag) => `${tag}\n    <script>${script}</script>`);
      },
    },
  };
}
