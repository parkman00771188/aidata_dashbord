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

function candidateBlock(candidates) {
  return candidates.map(c => {
    const cols = String(c.columns_text || '').split(/\s+/).filter(Boolean);
    const colText = cols.length
      ? cols.slice(0, 18).join(', ').slice(0, 320) + (cols.length > 18 ? ' …' : '') + ` (총 ${cols.length}개)`
      : '(항목 정보 없음)';
    return `- uid: ${c.uid} | 출처: ${c.source} | 데이터명: ${c.title} | 분야: ${c.field || '-'}`
      + `${c.subfield ? ' > ' + c.subfield : ''} | 제공기관: ${c.organization || '-'} | `
      + `형식: ${(c.formats || []).join(', ') || '-'} | 제공방식: ${(c.access && c.access.type) || '-'} | `
      + `행수: ${c.row_count || '-'} | 갱신: ${c.modified_at || '-'}\n`
      + `  데이터항목: ${colText}\n`
      + `  설명: ${String(c.description || '-').replace(/\s+/g, ' ').slice(0, 200)}`;
  }).join('\n');
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
  const hasSafezone = candidates.some(c => c.access && c.access.type === '안심존');

  const prompt = `[사용자 목적]
"""${query}"""

[후보 데이터 목록]
${candidateBlock(candidates)}
${hasSafezone ? '\n' + SAFEZONE_GUIDE + '\n' : ''}
위 후보만 사용해 아래 JSON 형식으로 답하라.
{
  "title": "이 추천을 나타내는 12자 이내 제목",
  "goal": "목적을 1~2문장으로 정리",
  "features": [
    {"name": "필요한 기능 이름", "detail": "무엇을 하는 기능인지 한두 문장", "data_need": "이 기능에 필요한 데이터 항목"}
  ],
  "datasets": [
    {"uid": "후보 목록의 uid", "role": "핵심|보조|참고",
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

규칙:
- datasets 는 중요도 순으로 최대 15개. '핵심'은 3~5개로 제한하되, 조금이라도 쓸모 있는 후보는 '보조'나 '참고'로라도 넣는다.
- items 는 반드시 해당 후보의 '데이터항목'에 실제로 있는 이름만 쓴다. 항목 정보가 없으면 items 를 비우고 why 에 "항목 미확인"이라고 적는다.
- 데이터항목을 보고 목적에 쓸 값이 없다고 판단되면 그 후보는 고르지 않는다.
- pipeline 의 detail 은 "A 데이터의 X 항목과 B 데이터의 Y 항목을 Z 기준으로 결합한다" 처럼 실제 데이터명과 항목명을 넣어 쓴다.
- 설명 문장에서는 uid 대신 사람이 읽는 데이터명을 쓴다.
- features 는 3~6개, pipeline 은 4~6단계로 만든다.${hasSafezone ? SAFEZONE_RULES : ''}`;

  let result, usage;
  try {
    const r = await geminiJSON(key, model, SYSTEM, prompt, 0.1);
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
    datasets.push({
      uid: row.uid, source: row.source, source_id: row.source_id, title: row.title,
      field: row.field || '', subfield: row.subfield || '', organization: row.organization || '',
      url: row.url || '', formats: row.formats || [], row_count: row.row_count || 0,
      downloads: row.downloads || 0, modified_at: row.modified_at || '', update_cycle: '',
      access: row.access, role: String(item.role || '참고').slice(0, 6),
      why: humanize(item.why, byUid), usage: humanize(item.usage, byUid),
      items: (item.items || []).map(String).slice(0, 8),
      join_key: String(item.join_key || ''),
      columns: String(row.columns_text || '').split(/\s+/).filter(Boolean).slice(0, 40),
      column_count: row.column_count || 0,
    });
  });

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
  return json({
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
  });
}
