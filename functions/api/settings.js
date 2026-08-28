import { json, bad, verifyRequest, readSettings, writeSettings, usingDefaultPassword,
         DEFAULT_MODEL } from '../_lib.js';

/** 브라우저에는 키 자체를 절대 내려보내지 않는다(마스킹만). */
function publicView(s, env, admin) {
  const key = s.key || env.GEMINI_API_KEY || '';
  return {
    has_key: !!key,
    key_hint: key ? '•'.repeat(8) + key.slice(-4) : '',
    key_source: s.key ? 'kv' : (env.GEMINI_API_KEY ? 'env' : ''),
    model: s.model || DEFAULT_MODEL,
    enabled: !!s.enabled,
    updated_at: s.updated_at || null,
    admin: !!admin,
    kv: !!s.kv,
    default_password: usingDefaultPassword(env),
    shared: true,
  };
}

export async function onRequestGet({ request, env }) {
  const admin = await verifyRequest(env, request);
  return json(publicView(await readSettings(env), env, admin));
}

export async function onRequestPost({ request, env }) {
  if (!(await verifyRequest(env, request))) {
    return bad('관리자 로그인이 필요합니다.', 401);
  }
  let body = {};
  try { body = await request.json(); } catch (e) {}
  const patch = {};
  if (body.enabled !== undefined) patch.enabled = !!body.enabled;
  if (body.model) patch.model = String(body.model).trim();
  if (body.clear_key) patch.key = '';
  else if (body.api_key && !String(body.api_key).includes('•')) patch.key = String(body.api_key).trim();
  if (!env.SETTINGS) return bad('KV 저장소(SETTINGS)가 연결되지 않았습니다. Cloudflare Pages 설정에서 KV 바인딩을 추가해 주세요.', 503);
  const saved = await writeSettings(env, patch);
  return json(publicView({ ...saved, kv: true }, env, true));
}
