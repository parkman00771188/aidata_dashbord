import { json, bad, activeKey, geminiJSON, estimateCost, DEFAULT_MODEL } from '../../_lib.js';

const SAFEZONE_GUIDE = `[안심존 데이터 이용 규칙 - 추천에 반드시 반영]
- 안심존은 데이터를 받아오는 곳이 아니라, 데이터가 있는 서버에 내 개발환경을 가져가
  학습시키고 결과물만 들고 나오는 곳이다.
- 밖으로 반출 가능: 소스코드, 학습된 AI 모델 파일뿐. 반출 불가: 원본·가공 데이터,
  CSV/NPY/PKL, 이미지·영상·음성·압축파일. 성능 결과도 간단한 그래프 수준만 심사를 거쳐 나온다.
- 신청: 열람이 아니라 '데이터 사용' 신청이며 IRB 승인과 연구계획서가 필요하다.
  여러 데이터를 조합할 계획이면 처음부터 '멀티데이터'로 함께 신청해야 한다.
- 서버: IRB 승인 연구 1건당 GPU 서버 1대, 사용기간 30일(주말·공휴일 포함).
  V100 32GB, 8 vCPU, RAM 90GB, Ubuntu 22.04, CUDA 12.2, Python 3.11 수준.
- 인터넷은 초기 최대 5일만 열린다. 그 사이에 패키지 설치·코드 업로드·자체 데이터 업로드를
  끝내야 하고, 데이터를 마운트하면 인터넷이 차단된다. 순서를 어기면 서버 초기화(약 1일)가 필요하다.
- 그래서 학습 코드는 밖에서 동일 스키마의 샘플·공개 데이터로 미리 완성해 두고 들어가야 한다.
- 원본 데이터 폴더는 읽기 전용이라 작업 폴더(nasw)로 복사해 쓴다. SHAP 등 설명가능성 분석도 내부에서 한다.
- 모델 파일에 학습 데이터가 들어가면 반출이 막힌다. 모델·feature_names·threshold·버전만 저장한다.
- 반출은 nasw/download 에 넣고 종료일 기준 최소 영업일 3일 전에 신청한다(심사 2~3영업일, 최대 3회).`;

const SAFEZONE_SCHEMA = `,
  "safezone_plan": {
    "datasets": ["안심존으로만 쓸 수 있는 추천 데이터의 uid"],
    "outside": ["안심존에 들어가기 전에 밖에서 준비할 일 3~5개"],
    "inside": ["안심존 안에서 순서대로 할 일 4~6개. 인터넷이 열린 초기 5일과 마운트 이후를 구분"],
    "combine": "다운로드 가능한 데이터와 안심존 데이터의 역할을 어떻게 나눌지 2~3문장",
    "export": ["30일 뒤 반출할 결과물. 반출 가능한 것만"],
    "schedule": [{"period": "Day 1~5", "task": "그 기간에 할 일"}],
    "cautions": ["안심존 때문에 특별히 조심할 점 2~4개"]
  }`;

const SAFEZONE_RULES = `
- 제공방식이 '안심존'인 데이터를 추천했다면 safezone_plan 을 반드시 채운다. 아니면 넣지 않는다.
- 안심존 데이터를 쓰는 단계는 '안심존 서버 안에서' 수행하고, 결과로 데이터가 아니라 모델·코드만 나온다는 점을 적는다.
- 다운로드 가능한 데이터는 밖에서 미리 코드·전처리를 검증하는 용도로 배치해 안심존 30일을 낭비하지 않게 한다.`;

const SYSTEM = '너는 데이터 기반 서비스 기획자다. 사용자의 목적과 후보 데이터 목록을 보고, '
  + '목적 달성에 필요한 기능을 정의하고 후보 중에서 실제로 쓸 데이터를 골라 활용 방법을 제시한다. '
  + "각 후보에는 실제 '데이터항목'(컬럼 이름)이 함께 주어진다. 데이터명이 그럴듯해도 "
  + '데이터항목에 목적에 필요한 값이 없으면 고르지 말고, 항목이 목적과 맞는 데이터를 우선한다. '
  + '반드시 후보 목록에 있는 uid만 사용하고, 목록에 없는 데이터를 지어내지 않는다. '
  + '모든 설명은 한국어로 쓰고 JSON만 출력한다.';

function candidateBlock(candidates, reasons) {
  reasons = reasons || {};
  return candidates.map(c => {
    const reason = c.source === 'AI Hub' ? (reasons[String(c.source_id || '')] || '') : '';
    const cols = String(c.columns_text || '').split(/\s+/).filter(Boolean);
    const colText = cols.length
      ? cols.slice(0, 18).join(', ').slice(0, 320) + (cols.length > 18 ? ' …' : '') + ` (총 ${cols.length}개)`
      : '(항목 정보 없음)';
    return `- uid: ${c.uid} | 출처: ${c.source} | 데이터명: ${c.title} | 분야: ${c.field || '-'}`
      + `${c.subfield ? ' > ' + c.subfield : ''} | 제공기관: ${c.organization || '-'} | `
      + `형식: ${(c.formats || []).join(', ') || '-'} | 제공방식: ${(c.access && c.access.type) || '-'} | `
      + `행수: ${c.row_count || '-'} | 갱신: ${c.modified_at || '-'}\n`
      + `  데이터항목: ${colText}\n`
      + `  설명: ${String(c.description || '-').replace(/\s+/g, ' ').slice(0, 200)}`
      + (reason ? `\n  ★ AI Hub 전문가 검토: 목적에 적합 - ${reason}` : '');
  }).join('\n');
}

/* 적합도 점수 -> 역할. 임계값을 코드에 고정해 같은 점수면 언제나 같은 역할이 나오게 한다.
 * (LLM 이 라벨을 직접 고르게 하면 같은 질문에도 매번 뒤바뀌었다.) */
const FIT_CORE = 70, FIT_SUB = 40, FIT_MIN = 20;
// 모델이 고르는 3단계. 역할과 1:1 로 대응해서 '몇 점을 줄까'라는 애매한 판단을 없앤다.
const FIT_LADDER = [90, 60, 30];
const roleOfFit = f => (f >= FIT_CORE ? '핵심' : f >= FIT_SUB ? '보조' : '참고');
function clampFit(value, fallbackRole) {
  let fit = Number(value);
  if (!Number.isFinite(fit)) {
    const map = { '핵심': 90, '보조': 60, '참고': 30 };
    return map[String(fallbackRole || '').trim()] || 60;
  }
  fit = Math.max(0, Math.min(100, fit));
  if (fit < FIT_MIN) return Math.round(fit);
  return FIT_LADDER.reduce((best, x) =>
    (Math.abs(x - fit) < Math.abs(best - fit) || (Math.abs(x - fit) === Math.abs(best - fit) && x > best)) ? x : best,
    FIT_LADDER[0]);
}

const UID_RE = /(?:aihub|public):[0-9]+/g;
function humanize(text, byUid) {
  if (!text) return '';
  let out = String(text).replace(UID_RE, m => (byUid[m] ? `'${byUid[m].title}'` : m));
  Object.values(byUid).forEach(row => {
    const t = row.title;
    if (!t || out.indexOf(t) < 0) return;
    const q = `'${t}'`;
    [`${t}(${q})`, `${t} (${q})`, `${q}(${q})`, `${q} (${q})`].forEach(d => { out = out.split(d).join(q); });
    out = out.split(`'${q}'`).join(q);
  });
  return out;
}

export async function onRequestPost({ request, env }) {
  let body = {};
  try { body = await request.json(); } catch (e) {}
  const query = String(body.query || '').trim();
  const candidates = Array.isArray(body.candidates) ? body.candidates.slice(0, 70) : [];
  if (query.length < 3) return bad('찾고 싶은 데이터를 조금 더 자세히 적어 주세요.');
  if (!candidates.length) return bad('후보 데이터가 없습니다.');

  const { key, settings } = await activeKey(env);
  if (!key) {
    return bad(settings.enabled
      ? 'AI 추천에 쓸 Gemini 키가 서버에 설정되어 있지 않습니다.'
      : 'AI 추천 기능이 꺼져 있습니다. 관리자가 설정에서 켜야 사용할 수 있습니다.', 503);
  }
  const model = settings.model || DEFAULT_MODEL;
  /* 같은 질문이면 같은 답이 나오도록 결과를 캐시한다.
   * Gemini 는 temperature 0 에서도 사고 과정이 매번 달라 역할(핵심/보조)이 뒤바뀌곤 했다.
   * 접두어의 버전을 올리면 옛 캐시는 자동으로 무시된다. */
  const recoKey = 'reco:fit-tier3-1:' + model + ':' + query.replace(/\s+/g, ' ').toLowerCase().slice(0, 300);
  if (env.SETTINGS && !body.refresh) {
    const hit = await env.SETTINGS.get(recoKey);
    if (hit) { try { return json({ ...JSON.parse(hit), cached: true }); } catch (e) {} }
  }
  const hasSafezone = candidates.some(c => c.access && c.access.type === '안심존');
  const plan = body.plan || {};
  const wantsAi = ['true', '1', 'yes', '예', 'y'].includes(String(plan.wants_ai_training).toLowerCase());
  const summaryLines = [];
  if (plan.region) summaryLines.push(`- 지역: ${plan.region} (이 지역 데이터를 우선)`);
  if (Array.isArray(plan.modality) && plan.modality.length) summaryLines.push(`- 원하는 데이터 형태: ${plan.modality.join(', ')}`);
  summaryLines.push(`- 목적: ${wantsAi ? 'AI 모델 학습·개발' : '현황·통계 분석/서비스 기획'}`);
  if (Array.isArray(plan.core) && plan.core.length) summaryLines.push(`- 핵심 개념: ${plan.core.join(', ')}`);
  const summary = `\n[요구 요약]\n${summaryLines.join('\n')}\n`;

  const prompt = `[사용자 목적]
"""${query}"""
${summary}
[후보 데이터 목록]
${candidateBlock(candidates, plan.aihub_pick_reasons)}
${hasSafezone ? '\n' + SAFEZONE_GUIDE + '\n' : ''}
위 후보만 사용해 아래 JSON 형식으로 답하라.
{
  "title": "이 추천을 나타내는 12자 이내 제목",
  "goal": "목적을 1~2문장으로 정리",
  "features": [
    {"name": "필요한 기능 이름", "detail": "무엇을 하는 기능인지 한두 문장", "data_need": "이 기능에 필요한 데이터 항목"}
  ],
  "datasets": [
    {"uid": "후보 목록의 uid", "fit": 목적 달성 기여도 점수(0~100 정수, 아래 채점 기준을 그대로 적용),
     "why": "이 목적에 왜 필요한지. 어떤 데이터항목 때문에 쓸 만한지 근거를 넣는다",
     "usage": "구체적인 활용 방법. 어떤 항목을 어떻게 가공/결합하는지",
     "items": ["실제로 쓸 데이터항목 3~6개. 후보의 '데이터항목'에 있는 이름 그대로"],
     "join_key": "다른 데이터와 연결할 때 쓸 공통 항목(없으면 빈 문자열)"}
  ],
  "pipeline": [
    {"step": "단계 이름(6자 내외)",
     "uses": ["이 단계에서 쓰는 데이터의 uid"],
     "detail": "이 데이터의 어떤 항목을 어떻게 쓰는지 2~3문장. 결합·가공 방법과 기준을 적는다",
     "output": "이 단계가 만들어 내는 결과물을 한 문장으로"}
  ],
  "outcome": "흐름을 끝냈을 때 실제로 무엇을 할 수 있게 되는지 2~3문장",
  "cautions": ["데이터 활용 시 유의사항 2~4개"],
  "missing": ["후보에 없어서 추가로 확보해야 할 데이터 1~3개"]${hasSafezone ? SAFEZONE_SCHEMA : ''}
}

[fit 채점 - 아래 세 값 중 하나만 쓴다. 다른 숫자는 절대 쓰지 않는다]
  90 (필수) : 이 데이터를 빼면 목적을 이룰 수 없다.
              학습·분석의 대상 그 자체이거나, 이 데이터에만 있는 입력이 반드시 필요하다.
  60 (보강) : 있으면 정확도나 적용 범위가 넓어진다. 빠져도 만들려는 것은 그대로 동작한다.
  30 (참고) : 직접 쓰지는 않는다. 방법론 참고나 결과 검증 비교용으로만 본다.
              지역만 다른 같은 종류의 데이터도 여기에 넣는다.
  세 가지 어디에도 해당하지 않으면 datasets 에 넣지 않는다.

판단 방법: 반드시 "이 데이터를 빼면 무엇이 불가능해지는가"만 묻는다.
  - 만들려는 것 자체가 불가능해진다        -> 90
  - 만들 수는 있는데 품질·범위가 떨어진다  -> 60
  - 아무것도 달라지지 않는다               -> 30
데이터가 좋아 보인다고 90 을 주지 않는다. 순위를 맞추려고 점수를 조정하지 않는다.
쓰임새가 같은 데이터에는 반드시 같은 점수를 준다.

규칙:
- datasets 는 최대 15개. 무관한 데이터를 넣어 개수를 채우지 않는다.
- 90점·60점을 준 데이터는 pipeline 의 uses 에도 등장시키는 것을 원칙으로 한다.
- 30점(참고)은 정말 방법론이나 결과 검증에 도움이 되는 것만 최대 2개까지 넣는다. 마땅한 것이 없으면 넣지 않는다. 억지로 채우지 않는다.
- 같은 성격의 데이터가 여러 건이면(예: 격자 크기만 다른 유동인구 데이터) 가장 알맞은 1~2개를 고른다.
- items 는 반드시 해당 후보의 '데이터항목'에 실제로 있는 이름만 쓴다. 항목 정보가 없는 후보는 데이터명·설명·행수로 판단하고 items 는 비우고 why 에 "항목 미확인"이라고 적는다. 항목 정보가 없다는 이유만으로 제외하지 않는다.
- 데이터항목이 있는데 목적에 쓸 값이 전혀 없다고 판단되면 그 후보는 고르지 않는다.
- '★ AI Hub 전문가 검토' 표시가 붙은 후보는 데이터명이 목적과 달라 보여도 내용이 맞는 것으로 확인된 것이다. 특별한 이유가 없으면 60점 이상을 주고, 그 근거를 why 에 반영한다.
- pipeline 의 detail 은 "A 데이터의 X 항목과 B 데이터의 Y 항목을 Z 기준으로 결합한다" 처럼 실제 데이터명과 항목명을 넣어 쓴다.
- 설명 문장에서는 uid 대신 사람이 읽는 데이터명을 쓴다.
- features 는 3~6개, pipeline 은 4~6단계로 만든다.
- 특정 지역이 지정된 요구라면 그 지역 데이터를 우선 고르고, 다른 지역의 같은 종류 데이터는 방법론 참고용으로 1~2개만 30점으로 넣는다.
- AI 학습 목적이고 원하는 형태가 이미지·음성·영상이면 그 형태의 학습 데이터(주로 AI Hub)에 90점을 주고, 통계·현황 데이터는 라벨·보조 정보로 보아 60점에 둔다.${hasSafezone ? SAFEZONE_RULES : ''}`;

  let result, usage;
  try {
    const r = await geminiJSON(key, model, SYSTEM, prompt, 0.0);
    result = r.data; usage = r.usage;
  } catch (e) {
    return bad(String(e.message || e), 502);
  }

  const byUid = {};
  candidates.forEach(c => { byUid[c.uid] = c; });

  const datasets = [];
  let dropped = 0;
  (result.datasets || []).slice(0, 16).forEach(item => {
    const row = byUid[String(item.uid || '').trim()];
    if (!row) { dropped++; return; }
    const fit = clampFit(item.fit, item.role);
    if (fit < FIT_MIN) return;
    datasets.push({
      uid: row.uid, source: row.source, source_id: row.source_id, title: row.title,
      field: row.field || '', subfield: row.subfield || '', organization: row.organization || '',
      url: row.url || '', formats: row.formats || [], row_count: row.row_count || 0,
      downloads: row.downloads || 0, modified_at: row.modified_at || '', update_cycle: '',
      access: row.access, fit, role: roleOfFit(fit),
      why: humanize(item.why, byUid), usage: humanize(item.usage, byUid),
      items: (item.items || []).map(String).slice(0, 8),
      join_key: String(item.join_key || ''),
      columns: String(row.columns_text || '').split(/\s+/).filter(Boolean).slice(0, 40),
      column_count: row.column_count || 0,
    });
  });

  // 점수 내림차순 + 같은 점수면 검색 단계의 순위(결정론적)로 정렬한다.
  const rankOf = new Map(candidates.map((c, i) => [c.uid, i]));
  datasets.sort((a, b) => (b.fit - a.fit)
    || ((rankOf.get(a.uid) ?? 999) - (rankOf.get(b.uid) ?? 999))
    || a.title.localeCompare(b.title, 'ko'));

  const locked = datasets.filter(d => d.access && d.access.type === '안심존');
  let safezone = null;
  if (locked.length) {
    const plan = result.safezone_plan || {};
    const list = (k, n) => (plan[k] || []).map(x => humanize(String(x), byUid)).slice(0, n);
    safezone = {
      datasets: locked.map(d => ({ uid: d.uid, title: d.title, source: d.source })),
      outside: list('outside', 6), inside: list('inside', 7),
      combine: humanize(String(plan.combine || ''), byUid),
      export: list('export', 5),
      schedule: (plan.schedule || []).slice(0, 8).map(s => ({
        period: String(s.period || ''), task: humanize(String(s.task || ''), byUid) })),
      cautions: list('cautions', 5),
    };
  }

  const id = (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : String(Date.now())).slice(0, 12);
  const payload = {
    id,
    created_at: new Date().toISOString().slice(0, 19).replace('T', ' '),
    query, model,
    title: String(result.title || query).slice(0, 60),
    goal: humanize(String(result.goal || body.goal || ''), byUid),
    keywords: body.keywords || [],
    fields: [],
    features: (result.features || []).slice(0, 8).map(f => ({
      name: String(f.name || ''), detail: humanize(String(f.detail || ''), byUid),
      data_need: humanize(String(f.data_need || ''), byUid) })),
    datasets,
    pipeline: (result.pipeline || []).slice(0, 8).map(p => ({
      step: String(p.step || ''), detail: humanize(String(p.detail || ''), byUid),
      output: humanize(String(p.output || ''), byUid),
      uses: (p.uses || []).filter(u => byUid[u]).slice(0, 6)
        .map(u => ({ uid: u, title: byUid[u].title, source: byUid[u].source })) })),
    outcome: humanize(String(result.outcome || ''), byUid),
    cautions: (result.cautions || []).map(String).slice(0, 6),
    missing: (result.missing || []).map(String).slice(0, 5),
    safezone,
    manual_url: 'https://aihub.or.kr/web-nas/aihub21/files/public/'
      + '%ED%97%AC%EC%8A%A4%EC%BC%80%EC%96%B4_%EC%95%88%EC%8B%AC%EC%A1%B4'
      + '%EC%82%AC%EC%9A%A9%EC%9E%90_%EB%A9%94%EB%89%B4%EC%96%BC.pdf',
    candidate_count: candidates.length,
    dropped,
    usage: { ...usage, calls: (usage.calls || 1) + 1, cost: estimateCost(model, usage) },
  };

  // 같은 질문이면 같은 답이 나오도록 저장해 둔다(30일).
  if (env.SETTINGS) {
    try {
      await env.SETTINGS.put(recoKey, JSON.stringify(payload), { expirationTtl: 60 * 60 * 24 * 30 });
    } catch (e) { /* 캐시 저장 실패는 추천 자체를 막지 않는다 */ }
  }
  return json(payload);
}
