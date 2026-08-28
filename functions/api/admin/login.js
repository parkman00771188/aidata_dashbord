import { json, bad, checkLogin, makeToken, sessionCookie, clearCookie, verifyRequest,
         readSettings, usingDefaultPassword, DEFAULT_ADMIN_USER } from '../../_lib.js';

export async function onRequestPost({ request, env }) {
  let body = {};
  try { body = await request.json(); } catch (e) {}
  if (body.logout) {
    return json({ ok: true, admin: false }, 200, { 'Set-Cookie': clearCookie() });
  }
  if (!(await checkLogin(env, body.user, body.password))) {
    return bad('아이디 또는 비밀번호가 올바르지 않습니다.', 401);
  }
  const token = await makeToken(env);
  const s = await readSettings(env);
  return json({
    ok: true, admin: true, enabled: !!s.enabled, model: s.model,
    has_key: !!(s.key || env.GEMINI_API_KEY), kv: !!s.kv,
    default_password: usingDefaultPassword(env), user: env.ADMIN_USER || DEFAULT_ADMIN_USER,
  }, 200, { 'Set-Cookie': sessionCookie(token) });
}

export async function onRequestGet({ request, env }) {
  const admin = await verifyRequest(env, request);
  const s = await readSettings(env);
  return json({
    admin, enabled: !!s.enabled, model: s.model,
    has_key: !!(s.key || env.GEMINI_API_KEY), kv: !!s.kv,
    default_password: usingDefaultPassword(env),
  });
}
