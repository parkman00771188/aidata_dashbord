/* Pages Functions 공통 유틸 - 설정 저장(KV), 관리자 로그인, Gemini 호출 */

export const DEFAULT_ADMIN_USER = 'admin';
export const DEFAULT_ADMIN_PASSWORD = 'admin123!@#';
export const DEFAULT_MODEL = 'gemini-flash-latest';
const SETTINGS_KEY = 'settings';
const COOKIE = 'aidata_admin';
const SESSION_HOURS = 12;

export function json(data, status = 200, headers = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers },
  });
}

export function bad(message, status = 400) {
  return json({ error: message }, status);
}

/** KV 가 연결돼 있지 않아도 빌드/미리보기가 죽지 않도록 감싼다. */
export async function readSettings(env) {
  const fallback = { enabled: false, model: DEFAULT_MODEL, key: '' };
  if (!env.SETTINGS) return { ...fallback, kv: false };
  try {
    const raw = await env.SETTINGS.get(SETTINGS_KEY);
    if (!raw) return { ...fallback, kv: true };
    return { ...fallback, ...JSON.parse(raw), kv: true };
  } catch (e) {
    return { ...fallback, kv: true };
  }
}

export async function writeSettings(env, patch) {
  const current = await readSettings(env);
  const next = {
    enabled: patch.enabled !== undefined ? !!patch.enabled : current.enabled,
    model: patch.model || current.model || DEFAULT_MODEL,
    key: patch.key !== undefined ? patch.key : current.key,
    updated_at: new Date().toISOString().slice(0, 19).replace('T', ' '),
  };
  if (env.SETTINGS) await env.SETTINGS.put(SETTINGS_KEY, JSON.stringify(next));
  return next;
}

/** 서버에 들어 있는 키(환경변수 우선, 없으면 KV). */
export async function activeKey(env) {
  const settings = await readSettings(env);
  if (!settings.enabled) return { key: '', settings };
  const key = settings.key || env.GEMINI_API_KEY || '';
  return { key, settings };
}

// ------------------------------------------------------------------ 관리자 인증
function adminUser(env) { return env.ADMIN_USER || DEFAULT_ADMIN_USER; }
function adminPassword(env) { return env.ADMIN_PASSWORD || DEFAULT_ADMIN_PASSWORD; }
export function usingDefaultPassword(env) { return !env.ADMIN_PASSWORD; }

function secret(env) {
  return env.ADMIN_SECRET || (adminUser(env) + ':' + adminPassword(env) + ':aidata');
}

async function hmac(env, message) {
  const enc = new TextEncoder();
  const cryptoKey = await crypto.subtle.importKey(
    'raw', enc.encode(secret(env)), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const sig = await crypto.subtle.sign('HMAC', cryptoKey, enc.encode(message));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, '0')).join('');
}

export async function makeToken(env) {
  const expires = Date.now() + SESSION_HOURS * 3600 * 1000;
  return expires + '.' + (await hmac(env, String(expires)));
}

export async function verifyRequest(env, request) {
  const cookie = request.headers.get('Cookie') || '';
  const m = cookie.match(new RegExp(COOKIE + '=([^;]+)'));
  if (!m) return false;
  const [expires, sig] = decodeURIComponent(m[1]).split('.');
  if (!expires || !sig) return false;
  if (Number(expires) < Date.now()) return false;
  const expected = await hmac(env, expires);
  if (expected.length !== sig.length) return false;
  let diff = 0;
  for (let i = 0; i < sig.length; i++) diff |= sig.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

export function sessionCookie(token) {
  return `${COOKIE}=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age=${SESSION_HOURS * 3600}`;
}

export function clearCookie() {
  return `${COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Secure; Max-Age=0`;
}

export async function checkLogin(env, user, password) {
  const okUser = String(user || '') === adminUser(env);
  const expected = adminPassword(env);
  const given = String(password || '');
  let diff = given.length === expected.length ? 0 : 1;
  for (let i = 0; i < Math.max(given.length, expected.length); i++) {
    diff |= (given.charCodeAt(i) || 0) ^ (expected.charCodeAt(i) || 0);
  }
  return okUser && diff === 0;
}

// ------------------------------------------------------------------ Gemini
const GEMINI = 'https://generativelanguage.googleapis.com/v1beta';

export async function geminiJSON(key, model, system, prompt, temperature = 0.2) {
  const url = `${GEMINI}/models/${encodeURIComponent(model || DEFAULT_MODEL)}:generateContent`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-goog-api-key': key },
    body: JSON.stringify({
      system_instruction: { parts: [{ text: system }] },
      contents: [{ role: 'user', parts: [{ text: prompt }] }],
      generationConfig: { temperature, responseMimeType: 'application/json' },
    }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data && data.error && data.error.message) || ('HTTP ' + res.status);
    if (res.status === 429) throw new Error('Gemini 호출 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.');
    if (res.status === 404) throw new Error('선택한 모델을 쓸 수 없습니다: ' + detail);
    if (res.status === 400 || res.status === 403) throw new Error('Gemini 키 또는 요청에 문제가 있습니다: ' + detail);
    throw new Error('Gemini 오류: ' + detail);
  }
  const cand = (data.candidates || [])[0];
  if (!cand) throw new Error('Gemini 가 응답을 생성하지 않았습니다.');
  const text = (cand.content && cand.content.parts || [])
    .filter(p => p && p.text && !p.thought).map(p => p.text).join('');
  if (!text.trim()) throw new Error('Gemini 응답이 비어 있습니다 (' + (cand.finishReason || '') + ')');
  const usage = data.usageMetadata || {};
  return {
    data: parseJSON(text),
    usage: {
      prompt: usage.promptTokenCount || 0,
      output: usage.candidatesTokenCount || 0,
      thoughts: usage.thoughtsTokenCount || 0,
      total: usage.totalTokenCount || 0,
      calls: 1,
    },
  };
}

export function parseJSON(text) {
  let t = String(text || '').trim().replace(/^```(?:json)?\s*|\s*```$/g, '');
  try { return JSON.parse(t); } catch (e) {}
  const a = t.indexOf('{'), b = t.lastIndexOf('}');
  if (a >= 0 && b > a) {
    try { return JSON.parse(t.slice(a, b + 1)); } catch (e) {}
  }
  throw new Error('Gemini 응답을 JSON 으로 해석하지 못했습니다.');
}

// 100만 토큰당 USD (ai_service.PRICING 과 같은 표)
const PRICING = {
  'gemini-2.5-pro': [1.25, 10.0], 'gemini-2.5-flash': [0.3, 2.5],
  'gemini-2.5-flash-lite': [0.1, 0.4], 'gemini-2.0-flash': [0.1, 0.4],
};
const TIER = { pro: [1.25, 10.0], flash: [0.3, 2.5], lite: [0.1, 0.4] };
export const USD_KRW = 1400;

export function estimateCost(model, usage) {
  const id = String(model || '').toLowerCase();
  let rate = PRICING[id], exact = true;
  if (!rate) {
    exact = false;
    rate = id.includes('-pro') ? TIER.pro : id.includes('flash-lite') ? TIER.lite : TIER.flash;
  }
  const usd = (usage.prompt / 1e6) * rate[0] + ((usage.output + usage.thoughts) / 1e6) * rate[1];
  return {
    usd: Math.round(usd * 1e6) / 1e6, krw: Math.round(usd * USD_KRW * 10) / 10,
    rate_in: rate[0], rate_out: rate[1], exact,
    basis: exact ? '공개 단가 기준' : '같은 등급 단가로 추정',
  };
}
