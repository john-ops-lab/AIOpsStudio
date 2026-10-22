// Regression for request-target origin confusion: no source/real credentials.
import assert from 'node:assert/strict';
import { createServer, request } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';

const received = [];
const api = createServer((req, res) => {
  received.push({ path: req.url, cookie: req.headers.cookie });
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify({ authenticated: true }));
});
api.listen(0, '127.0.0.1');
await once(api, 'listening');
const reserve = createServer();
reserve.listen(0, '127.0.0.1');
await once(reserve, 'listening');
const port = reserve.address().port;
await new Promise(resolve => reserve.close(resolve));
const web = spawn(process.execPath, ['server.mjs'], {
  cwd: fileURLToPath(new URL('../web/', import.meta.url)),
  env: { ...process.env, PORT: String(port), EASYOPS_API_URL: `http://127.0.0.1:${api.address().port}`,
    DO_NOT_TRACK: '1', COPILOTKIT_TELEMETRY_DISABLED: 'true' },
  stdio: ['ignore', 'pipe', 'pipe'],
});
function probe(path, host = `127.0.0.1:${port}`) {
  return new Promise((resolve, reject) => {
    const req = request({ host: '127.0.0.1', port, path,
      headers: { Host: host, Cookie: 'test-session=synthetic' } }, res => {
      res.resume(); res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', reject); req.end();
  });
}
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (web.exitCode !== null) throw new Error('Web process exited before readiness');
    try { await probe('/'); ready = true; break; } catch { await delay(50); }
  }
  assert.ok(ready, 'Mac test server ready');
  for (const path of ['//example.invalid/api/secret', 'http://example.invalid/api/secret', '/\\example.invalid/api/secret']) {
    assert.equal(await probe(path), 403, path);
  }
  assert.equal(await probe('/api/session', 'example.invalid'), 403);
  assert.equal(received.length, 0, 'rejected targets never reach upstream');
  assert.equal(await probe('/api/session?check=1'), 200);
  assert.deepEqual(received, [{ path: '/api/session?check=1', cookie: 'test-session=synthetic' }]);
  console.log('4 origin-confusion cases rejected; valid API target forwarded only to fixed local upstream');
} finally {
  web.kill('SIGTERM');
  if (web.exitCode === null) await once(web, 'exit');
  await new Promise(resolve => api.close(resolve));
}
