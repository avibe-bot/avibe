// Read-only preview of this branch's UI against the running local service.
// GETs proxy through to the real service so the screens show real config and
// status; every other verb is refused at the proxy, so browsing the preview can
// never write to the real config. Cookies are host-scoped, so the local auth
// session rides along. Not committed; delete when the preview is done.
import { defineConfig } from 'vite';
import base from './vite.config';

export default defineConfig({
  ...base,
  server: {
    port: 5210,
    strictPort: true,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5123',
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq, req, res) => {
            if (req.method && !['GET', 'HEAD'].includes(req.method)) {
              proxyReq.destroy();
              res.writeHead(403, { 'content-type': 'application/json' });
              res.end(JSON.stringify({ ok: false, message: 'preview is read-only' }));
            }
          });
        },
      },
    },
  },
});
