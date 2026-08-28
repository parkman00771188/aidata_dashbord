import { json, bad, activeKey, geminiJSON, DEFAULT_MODEL } from '../../_lib.js';

/* AI Hub 975건 제목을 통째로 보여 주고 목적에 맞는 것을 의미로 고른다.
 * AI Hub 데이터명은 '텍스트 윤리검증 데이터'처럼 개념적으로 지어져 문자열 검색으로는
 * 놓치기 쉽다. 목록이 작아서(약 15K 토큰) 이 방식이 가능하다.
 * 브라우저가 data/aihub-titles.json.gz 를 받아 함께 보낸다. */

const SYSTEM = '너는 AI 학습데이터 카탈로그(AI Hub) 전문가다. 사용자의 목적을 읽고 아래 데이터셋 목록에서 '
  + '실제로 학습·검증에 쓸 수 있는 것을 고른다. 데이터명이 목적과 다른 말로 쓰여 있어도 '
  + "내용상 맞으면 고른다(예: 욕설 필터 → '텍스트 윤리검증 데이터', 병해충 진단 → '작물 질병 진단 이미지'). "
  + '목록에 없는 것은 절대 만들지 않고 JSON만 출력한다.';

export async function onRequestPost({ request, env }) {
  let body = {};
  try { body = await request.json(); } catch (e) {}
  const query = String(body.query || '').trim();
  const titles = Array.isArray(body.titles) ? body.titles.slice(0, 1500) : [];
  if (query.length < 3) return bad('질문이 너무 짧습니다.');
  if (!titles.length) return json({ picks: [] });

  const { key, settings } = await activeKey(env);
  if (!key) return json({ picks: [] });

  const cacheKey = 'ap1:' + query.replace(/\s+/g, ' ').toLowerCase().slice(0, 300);
  if (env.SETTINGS) {
    const hit = await env.SETTINGS.get(cacheKey);
    if (hit) { try { return json({ ...JSON.parse(hit), cached: true }); } catch (e) {} }
  }

  const catalog = titles.map(t => `${t[0]} | ${String(t[1] || '').slice(0, 60)} | ${t[2] || ''} | ${(t[3] || []).join(',')}`).join('\n');
  const prompt = `[사용자 목적]
"""${query}"""
핵심 개념: ${(body.core || []).join(', ') || '-'}
원하는 형태: ${(body.modality || []).join(', ') || '-'}

[AI Hub 데이터셋 목록 - sn | 데이터명 | 분야 | 유형]
${catalog}

목적에 직접 쓸 수 있는 데이터셋의 sn 을 관련도 순으로 최대 8개 골라라. 없으면 빈 배열.
{"picks": [{"sn": "숫자", "why": "왜 맞는지 한 문장"}]}`;

  try {
    const { data, usage } = await geminiJSON(key, settings.model || DEFAULT_MODEL, SYSTEM, prompt, 0.0);
    const valid = new Set(titles.map(t => String(t[0])));
    const picks = [], reasons = {};
    (data.picks || []).slice(0, 8).forEach(p => {
      const sn = String(p.sn || '').replace(/\D/g, '');
      if (sn && valid.has(sn) && !picks.includes(sn)) { picks.push(sn); reasons[sn] = String(p.why || '').trim().slice(0, 120); }
    });
    const out = { picks, reasons, usage };
    if (env.SETTINGS) await env.SETTINGS.put(cacheKey, JSON.stringify(out), { expirationTtl: 60 * 60 * 24 * 30 });
    return json(out);
  } catch (e) {
    return json({ picks: [], error: String(e.message || e) });
  }
}
