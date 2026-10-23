// Keep legacy environment handling at the Node boundary; never send it to clients.
export function apiUpstream(environment = process.env) {
  const current = environment.AIOPSSTUDIO_API_URL;
  const legacy = environment.EASYOPS_API_URL;
  if (current !== undefined && legacy !== undefined && current !== legacy) {
    throw new Error('AIOPSSTUDIO_API_URL 与旧配置值冲突，请统一后再启动');
  }
  return current || legacy || 'http://127.0.0.1:8000';
}
