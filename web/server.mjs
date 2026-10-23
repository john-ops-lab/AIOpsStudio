import { createServer, request as httpRequest } from 'node:http';
import { AsyncLocalStorage } from 'node:async_hooks';
import { readFile } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { HttpAgent } from '@ag-ui/client';
import { AgentRunner, CopilotRuntime } from '@copilotkit/runtime/v2';
import { createCopilotNodeListener } from '@copilotkit/runtime/v2/node';
import { Observable } from 'rxjs';
import { apiUpstream } from './environment.mjs';

const upstream = new URL(apiUpstream());
const port = Number(process.env.PORT || 3000);
const origin = `http://127.0.0.1:${port}`;
const dist = resolve(fileURLToPath(new URL('./dist', import.meta.url)));
const context = new AsyncLocalStorage();

function apiHeaders() {
  const incoming = context.getStore() || {};
  return { 'Content-Type': 'application/json', Origin: origin, Cookie: incoming.cookie || '' };
}

function eventStream(path, input) {
  const headers = apiHeaders();
  return new Observable(observer => {
    const controller = new AbortController();
    (async () => {
      const response = await fetch(new URL(path, upstream), {
        method: 'POST', headers, body: JSON.stringify(input), signal: controller.signal,
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `分析协议请求失败 HTTP ${response.status}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let pending = '';
      while (true) {
        const { value, done } = await reader.read();
        pending += decoder.decode(value, { stream: !done }).replaceAll('\r\n', '\n');
        let boundary;
        while ((boundary = pending.indexOf('\n\n')) >= 0) {
          const block = pending.slice(0, boundary); pending = pending.slice(boundary + 2);
          const text = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (text) observer.next(JSON.parse(text));
        }
        if (done) break;
      }
      observer.complete();
    })().catch(error => { if (!controller.signal.aborted) observer.error(error); });
    return () => controller.abort(); // Unsubscribe never calls the business cancellation API.
  });
}

class PersistentRunner extends AgentRunner {
  run({ input }) { return eventStream('/api/ag-ui', input); }
  connect({ threadId }) {
    return eventStream('/api/ag-ui/connect', { threadId, runId: randomUUID(), messages: [], tools: [], context: [], state: {}, forwardedProps: {} });
  }
  async isRunning({ threadId }) {
    const response = await fetch(new URL(`/api/ag-ui/status/${encodeURIComponent(threadId)}`, upstream), { headers: apiHeaders() });
    if (!response.ok) return false;
    return Boolean((await response.json()).running);
  }
  async stop({ threadId, runId }) {
    const response = await fetch(new URL(`/api/ag-ui/stop/${encodeURIComponent(threadId)}`, upstream), {
      method: 'POST', headers: apiHeaders(), body: JSON.stringify({ runId }),
    });
    return response.ok;
  }
}

const runtime = new CopilotRuntime({
  agents: { aiopsstudio: new HttpAgent({ url: new URL('/api/ag-ui', upstream).href }) },
  runner: new PersistentRunner(),
});
const copilot = createCopilotNodeListener({ runtime, basePath: '/api/copilotkit', cors: false });
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.ico': 'image/x-icon', '.json': 'application/json' };

function fail(res, status, detail) {
  if (res.headersSent) return res.end();
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify({ detail }));
}

const server = createServer(async (req, res) => {
  try {
    if (req.headers.host !== `127.0.0.1:${port}` && req.headers.host !== `localhost:${port}`) {
      return fail(res, 403, '请通过配置的工作台地址访问');
    }
    // Only origin-form request targets are valid. URL resolution must never let
    // an incoming absolute/network-path URL replace the fixed API destination.
    if (!req.url?.startsWith('/') || req.url.startsWith('//')) return fail(res, 403, '请求地址必须属于当前工作台');
    const url = new URL(req.url, origin);
    if (url.origin !== origin) return fail(res, 403, '请求地址必须属于当前工作台');
    if (url.pathname.startsWith('/api/copilotkit')) {
      const session = await fetch(new URL('/api/session', upstream), { headers: { Cookie: req.headers.cookie || '' } });
      if (!session.ok || !(await session.json()).authenticated) return fail(res, 401, '请先登录工作台');
      if (!['GET', 'HEAD'].includes(req.method) && req.headers.origin !== origin) return fail(res, 403, '分析请求必须来自同源工作台');
      if (Number(req.headers['content-length'] || 0) > 2097152) return fail(res, 413, '请求正文不能超过2MiB');
      return context.run({ cookie: req.headers.cookie }, () => copilot(req, res));
    }
    if (url.pathname.startsWith('/api/') || url.pathname === '/healthz') {
      const target = new URL(upstream);
      target.pathname = url.pathname;
      target.search = url.search;
      const proxy = httpRequest(target, { method: req.method, headers: { ...req.headers, host: upstream.host } }, response => {
        res.writeHead(response.statusCode, { ...response.headers, 'X-Accel-Buffering': 'no' });
        response.pipe(res);
      });
      proxy.on('error', () => fail(res, 502, '工作台后端暂时不可用，请查看运行日志'));
      req.on('aborted', () => proxy.destroy());
      res.on('close', () => { if (!res.writableFinished) proxy.destroy(); });
      return req.pipe(proxy);
    }
    if (!['GET', 'HEAD'].includes(req.method)) return fail(res, 405, '不支持此请求方法');
    const pathname = decodeURIComponent(url.pathname);
    if (pathname.includes('\0') || pathname.split('/').some(part => part.startsWith('.'))) return fail(res, 404, '页面不存在');
    let path = resolve(dist, '.' + pathname);
    if (path !== dist && !path.startsWith(dist + sep)) return fail(res, 404, '页面不存在');
    let body;
    try { body = await readFile(path); }
    catch {
      if (extname(pathname)) return fail(res, 404, '文件不存在');
      path = resolve(dist, 'index.html'); body = await readFile(path);
    }
    res.writeHead(200, { 'Content-Type': mime[extname(path)] || 'application/octet-stream',
      'Cache-Control': path.endsWith('index.html') ? 'no-cache' : 'public,max-age=3600',
      'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin' });
    res.end(req.method === 'HEAD' ? undefined : body);
  } catch (error) {
    console.error('request_failed', error?.name || 'Error');
    fail(res, 500, '工作台请求失败，请查看服务日志');
  }
});
server.listen(port, '127.0.0.1', () => console.log(`AIOpsStudio 运维工作台 ${origin}`));
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => server.close(() => process.exit(0)));
