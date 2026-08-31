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

/* KV 캐시 키를 만든다.
 * 질문을 그대로 키에 넣으면 한글은 글자당 3바이트라 300자만 넣어도 900바이트가 되어
 * Cloudflare KV 의 키 길이 상한(512바이트)을 넘고, get/put 이 예외를 던져 500 이 난다.
 * 그래서 정규화한 질문을 SHA-256 으로 줄여 고정 길이 키를 쓴다. */
export async function cacheKey(prefix, ...parts) {
  const text = parts.map(v => String(v == null ? '' : v)).join('\u0000')
    .replace(/\s+/g, ' ').trim().toLowerCase();
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  const hex = Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, '0')).join('');
  return prefix + hex;
}

/* KV 는 없을 수도, 일시적으로 실패할 수도 있다. 캐시 때문에 요청 자체가 죽으면 안 된다. */
export async function cacheGet(env, key) {
  if (!env.SETTINGS) return null;
  try {
    const raw = await env.SETTINGS.get(key);
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    return null;
  }
}

export async function cachePut(env, key, value, ttlSeconds = 60 * 60 * 24 * 30) {
  if (!env.SETTINGS) return;
  try {
    await env.SETTINGS.put(key, JSON.stringify(value), { expirationTtl: ttlSeconds });
  } catch (e) { /* 캐시 저장 실패는 무시한다 */ }
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

/* 길이 제한으로 중간에 끊긴 JSON 을 닫아서 살린다.
 * Gemini 가 출력 한도에 걸리면 문자열이나 배열이 열린 채로 끝난다.
 * 앞부분(제목·목표·추천 데이터)은 멀쩡하므로 열려 있는 괄호를 닫아 복구한다. */
function repairJSON(text) {
  const start = text.indexOf('{');
  if (start < 0) return null;
  const t = text.slice(start);
  const stack = [];
  const cuts = [];        // [자를 위치, 그 시점의 열린 괄호] - 값이 온전히 끝난 지점들
  let inStr = false, esc = false;
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === '\\') esc = true;
      else if (ch === '"') { inStr = false; cuts.push([i + 1, stack.slice()]); }
      continue;
    }
    if (ch === '"') inStr = true;
    else if (ch === '{' || ch === '[') stack.push(ch === '{' ? '}' : ']');
    else if (ch === '}' || ch === ']') { stack.pop(); cuts.push([i + 1, stack.slice()]); }
    else if (/[0-9truefalsn]/.test(ch)) cuts.push([i + 1, stack.slice()]);
  }
  // 뒤에서부터 잘라 닫아 본다. 값 자리가 아니라 키 뒤에서 잘리면 파싱이 실패하므로
  // 성공할 때까지 한 칸씩 앞으로 물러난다.
  for (const [pos, open] of cuts.reverse().slice(0, 200)) {
    if (!open.length) continue;
    const body = t.slice(0, pos).replace(/\s+$/, '').replace(/,$/, '');
    try {
      const v = JSON.parse(body + open.slice().reverse().join(''));
      if (v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length) return v;
    } catch (e) { /* 더 앞으로 물러난다 */ }
  }
  return null;
}

export function parseJSON(text, reason) {
  let t = String(text || '').trim().replace(/^```(?:json)?\s*|\s*```$/g, '');
  try { return JSON.parse(t); } catch (e) {}
  const a = t.indexOf('{'), b = t.lastIndexOf('}');
  if (a >= 0 && b > a) {
    try { return JSON.parse(t.slice(a, b + 1)); } catch (e) {}
  }
  const repaired = repairJSON(t);            // 끊긴 응답을 닫아서 살려 본다
  if (repaired) return repaired;
  if (reason === 'MAX_TOKENS') {
    throw new Error('응답이 최대 길이를 넘어 잘렸습니다. 질문을 조금 줄이거나 다른 모델을 선택해 주세요.');
  }
  throw new Error('Gemini 응답을 JSON 으로 해석하지 못했습니다' + (reason ? ' (' + reason + ')' : '') + '.');
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
