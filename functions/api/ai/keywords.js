import { json, bad, activeKey, geminiJSON, DEFAULT_MODEL } from '../../_lib.js';

const SYSTEM = '너는 한국 공공데이터·AI 학습데이터 검색 전문가다. 사용자의 데이터 요구를 읽고 '
  + '국내 데이터 카탈로그(공공데이터포털, AI Hub)에서 검색할 한국어 키워드를 뽑는다. '
  + "카탈로그의 데이터명이나 컬럼명에 실제로 들어갈 법한 명사 위주로 만들고, '데이터'·'정보' 같은 일반어는 넣지 않는다. "
  + '반드시 JSON만 출력한다.';

const PROMPT = (query) => `사용자 요구:
"""${query}"""

아래 JSON 형식으로만 답하라.
{
  "goal": "요구를 한 문장으로 정리",
  "keywords": ["검색 키워드 16~22개, 중요한 것부터"]
}

키워드 규칙:
- 목적을 이루는 데 필요한 서로 다른 개념을 빠짐없이 넣는다(대상·현상·장소·시설·지표 등).
- 검색은 매우 빠르므로 넉넉하게 넣는다. 놓치는 것보다 조금 넓게 잡는 편이 낫다.
- 같은 뜻의 다른 표기를 반드시 함께 넣는다. 예) 전기차 / 전기자동차 / 친환경차,
  충전소 / 충전기 / 충전시설, 정신건강 / 심리 / 상담, 인구 / 유동인구 / 주민등록.
- 붙여 쓴 형태와 띄어 쓴 형태가 다르면 둘 다 넣는다. 예) 전기차충전소 / 전기차 충전소.
- 데이터가 담길 만한 기관·제도 이름도 넣는다. 예) 정신건강복지센터, 청소년상담복지센터.
- 조사·서술어와 '데이터', '정보' 같은 일반어는 넣지 않는다.`;

export async function onRequestPost({ request, env }) {
  let body = {};
  try { body = await request.json(); } catch (e) {}
  const query = String(body.query || '').trim();
  if (query.length < 3) return bad('찾고 싶은 데이터를 조금 더 자세히 적어 주세요.');

  const { key, settings } = await activeKey(env);
  if (!key) {
    return bad(settings.enabled
      ? 'AI 추천에 쓸 Gemini 키가 서버에 설정되어 있지 않습니다.'
      : 'AI 추천 기능이 꺼져 있습니다. 관리자가 설정에서 켜야 사용할 수 있습니다.', 503);
  }
  // 같은 질문이면 같은 키워드를 쓰도록 KV 에 캐시한다(결과가 흔들리지 않게).
  const cacheKey = 'kw:' + query.replace(/\s+/g, ' ').toLowerCase().slice(0, 300);
  if (env.SETTINGS) {
    const hit = await env.SETTINGS.get(cacheKey);
    if (hit) {
      try { return json({ ...JSON.parse(hit), cached: true }); } catch (e) {}
    }
  }
  try {
    const { data, usage } = await geminiJSON(key, settings.model || DEFAULT_MODEL,
      SYSTEM, PROMPT(query), 0.0);
    const keywords = (data.keywords || []).map(k => String(k).trim()).filter(Boolean).slice(0, 22);
    const out = { goal: String(data.goal || ''), keywords, usage };
    if (env.SETTINGS && keywords.length) {
      await env.SETTINGS.put(cacheKey, JSON.stringify({ goal: out.goal, keywords, usage }),
        { expirationTtl: 60 * 60 * 24 * 30 });
    }
    return json(out);
  } catch (e) {
    return bad(String(e.message || e), 502);
  }
}
