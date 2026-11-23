import { useTranslation } from 'react-i18next';
import { useTheme } from '@/context/ThemeContext';

import serverSource from '../../../vibe/ui_server.py?raw';
import english from '../../../vibe/i18n/en.json';
import chinese from '../../../vibe/i18n/zh.json';

const script = serverSource.match(/_SHOW_SYSTEM_ENTRY_SCRIPT_BODY = r"""([\s\S]*?)"""/)?.[1];
if (!script) throw new Error('Show system-entry product script is missing');

/** Real injected product UI in a synthetic Show document. Only browser topology,
 *  platform, annotation state and persisted preference are fixtures; no UI is redrawn. */
export function ShowSystemEntryFixture({ platform, state }: {
  platform: 'ios' | 'mac';
  state: 'collapsed' | 'expanded' | 'annotating';
}) {
  const { i18n } = useTranslation();
  const { resolvedTheme } = useTheme();
  const zh = i18n.language.startsWith('zh');
  const copy = (zh ? chinese : english).show.systemEntry;
  const config = JSON.stringify({ copy }).replaceAll('<', '\\u003c');
  const agent = platform === 'ios'
    ? 'Mozilla/5.0 (iPhone) Version/18.0 Mobile Safari/605.1.15'
    : 'Mozilla/5.0 (Macintosh) Version/18.0 Safari/605.1.15';
  const document = `<!doctype html><html style="color-scheme: ${resolvedTheme}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{margin:0;font:15px/1.6 system-ui;background:Canvas;color:CanvasText}
main{box-sizing:border-box;min-height:calc(100vh - 320px);max-width:900px;margin:auto;padding:24px;display:flex;flex-direction:column}
article{border:1px solid GrayText;border-radius:16px;padding:20px}
small{opacity:.7}#last-line{margin-top:auto;padding-top:20px;font-size:13px}
#annotation{position:fixed;right:26px;bottom:26px;width:52px;height:52px;border:1px solid GrayText;border-radius:16px;background:Canvas;color:CanvasText;z-index:2147483200}
#annotation-panel{position:fixed;bottom:0;left:0;right:0;height:60vh;background:Canvas;border-top:1px solid GrayText;z-index:2147483300}
</style></head><body><main>
<small>Show Page · ${zh ? '合成测试内容' : 'Synthetic test content'}</small>
<h1>${zh ? '健康接入流程' : 'Health connection flow'}</h1>
<article>${zh ? '作者内容与添加指引分别可见。' : 'Authored content and installation guidance remain visible.'}</article>
<p id="last-line">${zh ? '页面末行 — 展开指引后仍完整可见。' : 'END OF TEST CONTENT — this line must remain reachable.'}</p>
</main><button id="annotation" data-show-annotation-ui aria-label="Annotate">✎</button>
${state === 'annotating' ? '<section id="annotation-panel" role="dialog" aria-label="Annotation">Annotation</section>' : ''}
<script>
Object.defineProperty(navigator, 'userAgent', {value: ${JSON.stringify(agent)}});
const meta = document.createElement('meta');
meta.name = 'avibe-show-system-entry';
meta.content = ${JSON.stringify(config)};
document.head.append(meta);
const values = new Map();
const preferenceKey = 'avibe-show-system-entry:' + location.pathname;
values.set(preferenceKey, ${JSON.stringify(state === 'annotating' ? 'expanded' : state)});
window.__AVIBE_SHOW__ = { annotation: { api: {
  getState: () => ({ enabled: ${state === 'annotating'}, available: true }),
  subscribe: () => () => {}
} } };
// The canvas embeds every frame. Model the intended top-level Show environment
// here; separate browser tests exercise real top-level and iframe behavior.
const fixtureWindow = new Proxy(window, { get(target, key) {
  if (key === 'top' || key === 'self') return target;
  if (key === 'localStorage') return { getItem: k => values.get(k), setItem: (k,v) => values.set(k,v) };
  const value = Reflect.get(target, key);
  return typeof value === 'function' ? value.bind(target) : value;
} });
(function(window) { ${script} })(fixtureWindow);
</script></body></html>`;
  return (
    <iframe
      title="Show system entry product fixture"
      className="h-full w-full border-0"
      style={{ colorScheme: resolvedTheme }}
      srcDoc={document}
    />
  );
}
