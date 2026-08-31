import { json, bad, activeKey, geminiJSON, DEFAULT_MODEL, cacheKey, cacheGet, cachePut } from '../../_lib.js';

const SYSTEM = '너는 한국 공공데이터·AI 학습데이터 검색 전문가다. 사용자의 데이터 요구를 읽고 '
  + '국내 데이터 카탈로그(공공데이터포털, AI Hub)에서 검색할 한국어 키워드를 뽑는다. '
  + "카탈로그의 데이터명이나 컬럼명에 실제로 들어갈 법한 명사 위주로 만들고, '데이터'·'정보' 같은 일반어는 넣지 않는다. "
  + '반드시 JSON만 출력한다.';

const PROMPT = (query) => `사용자 요구:
"""${query}"""

아래 JSON 형식으로만 답하라.
{
  "goal": "요구를 한 문장으로 정리",
  "core": ["반드시 데이터에 들어 있어야 하는 핵심 개념 3~6개. 이것이 없는 데이터는 쓸모가 없다"],
  "related": ["핵심을 넓혀 주는 동의어·표기변형·관련 지표·기관명 10~16개"],
  "region": "요구에 특정 지역이 있으면 그 지역명(예: 제주, 부산, 서울). 없으면 빈 문자열",
  "modality": ["원하는 데이터 형태. 이미지 / 텍스트 / 음성 / 영상 / 정형 중 해당하는 것만"],
  "wants_ai_training": "AI 모델을 학습·개발하려는 목적이면 true, 통계·현황 파악이면 false"
}

규칙:
- core 는 '이 데이터가 없으면 목적을 이룰 수 없다'는 개념만 넣는다. 예) 병해충 진단 AI → 병해충, 작물, 이미지
- related 는 넉넉히 넣되 core 와 무관한 일반어(말뭉치, 데이터, 현황, 정보)는 넣지 않는다.
- 같은 뜻의 다른 표기를 함께 넣는다. 예) 전기차/전기자동차/친환경차, 충전소/충전기, 정신건강/심리/상담.
- 붙여 쓴 형태와 띄어 쓴 형태가 다르면 둘 다 넣는다. 예) 전기차충전소 / 전기차 충전소.
- 데이터가 담길 만한 기관·제도 이름도 related 에 넣는다. 예) 정신건강복지센터, 농촌진흥청.
- 모든 키워드는 카탈로그의 데이터명이나 컬럼명에 실제로 등장할 법한 2~6글자 한국어 명사로 쓴다.
- AI 학습데이터는 '윤리검증', '질병 진단', '감성 대화'처럼 중립적·기술적 이름을 쓰는 경우가 많다.
  민감하거나 구어적인 주제(욕설·혐오·사고·질병 등)는 데이터명이 쓸 법한 중립어도 related 에 넣는다.
- 조사·서술어('하는', '위한', '만들고')와 '데이터', '정보' 같은 일반어는 넣지 않는다.`;

const STOP = new Set(['데이터', '데이터셋', '자료', '정보', '관련', '필요', '활용', '위한', '위해', '대한',
  '현황', '말뭉치', '코퍼스', '전체', '모든', '여러', '다양한', '기반', '구축', '사용', '이용', '분석']);
const clean = (arr, n) => (Array.isArray(arr) ? arr : []).map(k => String(k).trim())
  .filter(k => k && !STOP.has(k)).slice(0, n);
const truthy = v => ['true', '1', 'yes', '예', 'y'].includes(String(v).trim().toLowerCase());

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
  // 같은 질문이면 같은 검색 계획을 쓰도록 KV 에 캐시한다(결과가 흔들리지 않게).
  // 형식이 바뀌면 접두어를 올려 옛 캐시를 무시한다.
  const ckey = await cacheKey('kw3:', query);
  const hit = await cacheGet(env, ckey);
  if (hit && (hit.core || hit.related)) return json({ ...hit, cached: true });
  try {
    const { data, usage } = await geminiJSON(key, settings.model || DEFAULT_MODEL, SYSTEM, PROMPT(query), 0.0);
    const plan = {
      goal: String(data.goal || ''),
      core: clean(data.core, 8),
      related: clean(data.related, 18),
      region: String(data.region || '').trim(),
      modality: clean(data.modality, 5),
      wants_ai_training: truthy(data.wants_ai_training),
      usage,
    };
    plan.keywords = plan.core.concat(plan.related);
    if (!plan.keywords.length) return bad('질문에서 검색어를 뽑지 못했습니다. 조금 더 구체적으로 적어 주세요.');
    await cachePut(env, ckey, plan);
    return json(plan);
  } catch (e) {
    return bad(String(e.message || e), 502);
  }
}
