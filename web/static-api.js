/* Cloudflare Pages 정적 모드 데이터 계층.
 *
 * 로컬에서는 serve.py 가 /api/* 를 처리하지만, Pages 에는 파이썬 서버가 없다.
 * 그래서 같은 응답 모양을 정적 JSON 샤드로 만들어 돌려준다.
 * catalog.html 은 이 파일이 있으면 자동으로 이쪽을 쓴다.
 *
 *   목록/필터/정렬/페이지  : data/idx-*.json (검색 색인) + data/page-*.json (첫 화면)
 *   상세                   : data/det-*.json
 *   내 데이터 / 저장된 추천 : 브라우저 localStorage
 *   AI 추천 / 설정          : Cloudflare Pages Functions (/api/ai/*, /api/settings)
 */
(function () {
  'use strict';

  const DATA = 'data/';
  const LS_MY = 'catalog_my_static';
  const LS_RECO = 'catalog_reco_static';

  let META = null;                 // meta.json
  let INDEX = null;                // 전체 검색 색인 (지연 로드)
  let indexPromise = null;
  let shardMap = null;             // uid -> 상세 샤드 번호
  const detailCache = new Map();
  const listeners = [];

  function onProgress(fn) { listeners.push(fn); }
  function emit(state) { listeners.forEach(fn => { try { fn(state); } catch (e) {} }); }
  window.STATIC_PROGRESS = onProgress;

  /* 데이터 샤드는 gzip 으로 올려 두고 브라우저에서 직접 푼다.
   * Content-Encoding 헤더를 쓰면 CDN 이 한 번 더 압축해 이중 압축이 되므로
   * 원시 바이트로 받아 DecompressionStream 으로 해제한다. */
  async function getJSON(path) {
    const r = await fetch(DATA + path + '.gz', { cache: 'force-cache' });
    if (!r.ok) throw new Error(path + ' 를 불러오지 못했습니다 (' + r.status + ')');
    const bytes = new Uint8Array(await r.arrayBuffer());
    if (bytes[0] === 0x1f && bytes[1] === 0x8b) {
      if (typeof DecompressionStream === 'undefined') {
        throw new Error('이 브라우저는 압축 해제를 지원하지 않습니다. 최신 브라우저를 사용해 주세요.');
      }
      const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
      return JSON.parse(await new Response(stream).text());
    }
    return JSON.parse(new TextDecoder().decode(bytes));   // 이미 풀려서 온 경우
  }

  async function meta() {
    if (!META) META = await getJSON('meta.json');
    return META;
  }

  /** 검색·필터에 필요한 전체 색인을 처음 필요할 때 한 번만 받는다. */
  function loadIndex() {
    if (INDEX) return Promise.resolve(INDEX);
    if (indexPromise) return indexPromise;
    indexPromise = (async () => {
      const m = await meta();
      const n = (m.shards && m.shards.index) || 12;
      emit({ loading: true, done: 0, total: n });
      const parts = new Array(n);
      let done = 0;
      await Promise.all(Array.from({ length: n }, (_, i) =>
        getJSON('idx-' + String(i).padStart(2, '0') + '.json')
          .then(rows => { parts[i] = rows; emit({ loading: true, done: ++done, total: n }); })
          .catch(() => { parts[i] = []; emit({ loading: true, done: ++done, total: n }); })));
      INDEX = [].concat.apply([], parts);
      INDEX.forEach(it => {
        it._hay = (it.t + ' ' + it.o + ' ' + it.mt + ' ' + it.d).toLowerCase();
        it._title = it.t.toLowerCase();
        it._meta = it.mt.toLowerCase();
      });
      emit({ loading: false, done: n, total: n, count: INDEX.length });
      return INDEX;
    })();
    return indexPromise;
  }
  window.STATIC_LOAD_INDEX = loadIndex;

  const SOURCE_NAME = { aihub: 'AI Hub', public: '공공데이터포털' };
  const AVAIL_ORDER = ['다운로드', '준비중', '안심존', '불가'];

  function toRow(it) {
    return {
      uid: it.uid, source: it.s ? 'AI Hub' : '공공데이터포털', source_id: it.sid,
      title: it.t, file_name: it.t, field: it.f, subfield: it.sf,
      organization: it.o, formats: it.fm, keywords: [],
      description: it.d, modified_at: it.m, created_at: it.c,
      update_cycle: it.uc, row_count: it.rc, views: it.vw, downloads: it.dl,
      url: it.u, preview_status: it.ps, detail_status: 'ok',
      access: it.a, column_count: it.cn,
      size_bytes: (it.a && it.a.size_bytes) || 0,
    };
  }

  function matches(it, q) {
    for (let i = 0; i < q.length; i++) if (it._hay.indexOf(q[i]) < 0) return false;
    return true;
  }

  function filterIndex(params) {
    const source = params.get('source');
    const fields = params.getAll('field');
    const formats = params.getAll('format').map(x => x.toUpperCase());
    const preview = params.get('preview') || '';
    const terms = (params.get('q') || '').toLowerCase().split(/\s+/).filter(Boolean);
    const wantSource = SOURCE_NAME[source] || '';
    return INDEX.filter(it => {
      if (wantSource) { const s = it.s ? 'AI Hub' : '공공데이터포털'; if (s !== wantSource) return false; }
      if (fields.length && fields.indexOf(it.f) < 0) return false;
      if (formats.length && !formats.some(f => it.fm.indexOf(f) >= 0)) return false;
      if (preview === 'yes' && it.ps !== 'ok') return false;
      if (preview === 'no' && ['ok', 'not_applicable'].indexOf(it.ps) >= 0) return false;
      if (terms.length && !matches(it, terms)) return false;
      return true;
    });
  }

  function sortRows(rows, sort, dir) {
    const sign = dir === 'asc' ? 1 : -1;
    const key = {
      title: it => it.t, field: it => it.f, organization: it => it.o,
      modified_at: it => it.m, row_count: it => it.rc, views: it => it.vw,
      downloads: it => it.dl, list_order: it => it.m,
    }[sort] || (it => it.m);
    return rows.slice().sort((a, b) => {
      const x = key(a), y = key(b);
      if (typeof x === 'number' && typeof y === 'number') return (x - y) * sign;
      return String(x).localeCompare(String(y), 'ko') * sign;
    });
  }

  function isDefaultView(params) {
    return !params.get('q') && !params.getAll('field').length && !params.getAll('format').length
      && !params.get('preview') && !params.get('source')
      && (params.get('sort') || 'modified_at') === 'modified_at'
      && (params.get('dir') || 'desc') === 'desc';
  }

  async function catalogList(params) {
    const m = await meta();
    const page = Math.max(1, parseInt(params.get('page') || '1', 10));
    const per = Math.min(200, Math.max(10, parseInt(params.get('per') || '50', 10)));

    // 아무 조건 없는 첫 화면은 미리 만들어 둔 페이지 파일로 즉시 응답한다.
    if (isDefaultView(params) && per % m.shards.page_size === 0) {
      const need = per / m.shards.page_size;
      const first = (page - 1) * need;
      if (first + need <= m.shards.pages) {
        const chunks = await Promise.all(Array.from({ length: need }, (_, i) =>
          getJSON('page-' + String(first + i).padStart(4, '0') + '.json')));
        const rows = [].concat.apply([], chunks).map(toRow);
        return { total: m.total, page: page, per: per, items: rows };
      }
    }
    await loadIndex();
    const rows = sortRows(filterIndex(params), params.get('sort') || 'modified_at', params.get('dir') || 'desc');
    return {
      total: rows.length, page: page, per: per,
      items: rows.slice((page - 1) * per, page * per).map(toRow),
    };
  }

  async function catalogFacets(params) {
    const m = await meta();
    const source = params.get('source');
    if (!source) return { fields: m.fields, formats: m.formats };
    await loadIndex();
    const want = SOURCE_NAME[source];
    const f = new Map(), g = new Map();
    INDEX.forEach(it => {
      const s = it.s ? 'AI Hub' : '공공데이터포털';
      if (s !== want) return;
      if (it.f) f.set(it.f, (f.get(it.f) || 0) + 1);
      it.fm.forEach(x => g.set(x, (g.get(x) || 0) + 1));
    });
    const sorted = mp => [...mp.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], 'ko'))
      .map(([value, count]) => ({ value, count }));
    return { fields: sorted(f).slice(0, 60), formats: sorted(g).slice(0, 40) };
  }

  async function catalogDetail(params) {
    const uid = params.get('uid') || '';
    const m = await meta();
    if (!shardMap) shardMap = await getJSON('shard-map.json');
    const n = shardMap[uid];
    if (n === undefined) throw new Error('데이터를 찾을 수 없습니다');
    const name = 'det-' + String(n).padStart(4, '0') + '.json';
    if (!detailCache.has(name)) detailCache.set(name, await getJSON(name));
    const shard = detailCache.get(name);
    const row = shard[uid];
    if (!row) throw new Error('데이터를 찾을 수 없습니다');
    return row;
  }

  // ---------------------------------------------------------------- 내 데이터 / 추천 보관함
  function readLS(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch (e) { return fallback; }
  }
  function writeLS(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {}
  }

  // ---------------------------------------------------------------- AI 추천용 검색
  const STOPWORDS = new Set(['데이터', '데이터셋', '자료', '정보', '관련', '필요', '활용', '위한', '위해',
    '대한', '있는', '하는', '해서', '추천', '전체', '모든', '여러', '다양한', '기반', '구축', '사용', '이용', '분석',
    '합니다', '입니다', '하려고', '만들고', '싶습니다', '있습니다', '찾고', '통해', '결합', '어떤', '경우', '방법']);

  // 지역 접두어 - 같은 데이터의 지역별 복제본을 묶고, 지역 지정 질문에서 다른 지역을 뒤로 보낸다.
  const REGION_WORDS = ['서울', '부산', '대구', '인천', '광주', '대전', '울산', '세종', '경기', '강원',
    '충북', '충남', '충청', '전북', '전남', '전라', '경북', '경남', '경상', '제주'];
  const REGION_ALIASES = {
    '서울': ['서울'], '부산': ['부산'], '대구': ['대구'], '인천': ['인천'], '광주': ['광주'], '대전': ['대전'],
    '울산': ['울산'], '세종': ['세종'], '경기': ['경기'], '강원': ['강원'], '충북': ['충북', '충청북도'],
    '충남': ['충남', '충청남도'], '전북': ['전북', '전라북도'], '전남': ['전남', '전라남도'],
    '경북': ['경북', '경상북도'], '경남': ['경남', '경상남도'], '제주': ['제주'],
  };
  const MEDIA_WORDS = { '이미지': ['이미지', '영상', 'jpg', 'png', 'image'], '영상': ['영상', '비디오', 'video', 'mp4'],
    '음성': ['음성', '오디오', 'wav', 'audio'], '텍스트': ['텍스트', '말뭉치', 'text', 'json'] };
  function regionTerms(region) {
    region = String(region || '').trim();
    if (!region) return [];
    for (const [key, aliases] of Object.entries(REGION_ALIASES)) {
      if (region.startsWith(key) || aliases.some(a => region.startsWith(a))) return aliases;
    }
    return region.length >= 2 ? [region.slice(0, 2)] : [];
  }
  function titleGroupKey(title) {
    let t = String(title || '').replace(/^[^_]{2,40}_/, '');
    REGION_WORDS.forEach(w => { t = t.split(w).join(''); });
    return t.replace(/[\s\d()\[\]·\-_,./]+/g, '').toLowerCase().slice(0, 40);
  }
  function hasOtherRegion(title, wanted) {
    const head = String(title || '').slice(0, 14);
    if (wanted.some(w => head.includes(w))) return false;
    return REGION_WORDS.some(w => head.includes(w));
  }
  const truthy = v => ['true', '1', 'yes', '예', 'y'].includes(String(v).trim().toLowerCase());

  /** ai_service.search_candidates 의 브라우저 판.
   *  plan: { core, related, region, modality, wants_ai_training } (키워드 단계 결과) */
  async function searchCandidates(plan, limit) {
    await loadIndex();
    limit = limit || 120;
    plan = plan || {};
    const core = (plan.core || []).map(k => String(k).trim()).filter(Boolean);
    let related = (plan.related || []).map(k => String(k).trim()).filter(Boolean);
    if (!core.length && !related.length) related = (plan.keywords || []).map(String);
    const kws = [], isCore = [];
    const add = (k, c) => {
      k = String(k || '').trim();
      if (k.length < 2 || STOPWORDS.has(k) || kws.indexOf(k) >= 0) return;
      kws.push(k); isCore.push(c);
      const ns = k.replace(/\s+/g, '');
      if (ns !== k && ns.length >= 2 && kws.indexOf(ns) < 0) { kws.push(ns); isCore.push(c); }
    };
    core.forEach(k => add(k, true));
    related.forEach(k => add(k, false));
    if (!kws.length) return { candidates: [], stats: null };
    const hasCore = isCore.some(Boolean);
    const low = kws.map(k => k.toLowerCase());
    const wantedRegion = regionTerms(plan.region);
    const wantsAi = truthy(plan.wants_ai_training);
    const modality = new Set((plan.modality || []).map(String));

    const hits = low.map(() => new Map());
    const regionSet = new Set();
    INDEX.forEach(it => {
      for (let i = 0; i < low.length; i++) {
        const kw = low[i];
        let s = 0;
        if (it._title.indexOf(kw) >= 0) s += 6;
        if (it._meta.indexOf(kw) >= 0) s += 3;
        if (s === 0 && it._hay.indexOf(kw) >= 0) s += 1.5;
        if (s) hits[i].set(it.uid, s);
      }
      if (wantedRegion.length && wantedRegion.some(w => it.t.includes(w) || it.o.includes(w))) regionSet.add(it.uid);
    });
    const matched = new Set([].concat.apply([], hits.map(h => [...h.keys()])));
    regionSet.forEach(u => matched.add(u));
    const totalRows = Math.max(1, matched.size);
    const idf = hits.map(h => Math.max(0.35, Math.log((totalRows + 1) / (h.size + 1)) / Math.log(20) + 0.35));

    const combined = new Map();   // uid -> [score, coreHits, relatedHits, coreScore, relScore]
    hits.forEach((h, i) => h.forEach((raw, uid) => {
      const slot = combined.get(uid) || [0, 0, 0, 0, 0];
      const w = raw * idf[i] * (isCore[i] ? 2 : 1);
      slot[0] += w;
      slot[isCore[i] ? 1 : 2] += 1;
      slot[isCore[i] ? 3 : 4] += w;
      combined.set(uid, slot);
    }));
    regionSet.forEach(uid => { if (!combined.has(uid)) combined.set(uid, [0, 0, 0, 0, 0]); });
    const semantic = new Set((plan.aihub_picks || []).map(x => 'aihub:' + String(x).replace('aihub:', '')));
    semantic.forEach(uid => { if (!combined.has(uid)) combined.set(uid, [0, 1, 0, 0, 0]); matched.add(uid); });
    const byUid = new Map(INDEX.map(it => [it.uid, it]));
    const baseRank = s => s[0] + s[1] * 6 + s[2] * 1.5;
    const ranked = [...combined.entries()].sort((a, b) => baseRank(b[1]) - baseRank(a[1]));

    const chosen = [], seen = new Set();
    semantic.forEach(uid => { if (byUid.has(uid)) { seen.add(uid); chosen.push(uid); } });  // 의미 선별은 반드시 포함
    ranked.slice(0, 440).forEach(([uid]) => { if (!seen.has(uid)) { seen.add(uid); chosen.push(uid); } });
    hits.forEach(h => [...h.entries()].sort((a, b) => b[1] - a[1]).slice(0, 30)
      .forEach(([uid]) => { if (!seen.has(uid)) { seen.add(uid); chosen.push(uid); } }));

    const scored = [], fallback = [];
    chosen.forEach(uid => {
      const it = byUid.get(uid); if (!it) return;
      const [, coreHits, relHits, coreScore, relScore] = combined.get(uid) || [0, 0, 0, 0, 0];
      const isSemantic = semantic.has(uid);
      let relaxed = false;
      if (!isSemantic) {
        if (!hasCore && relHits === 0) return;
        if (hasCore && coreHits === 0) { if (relHits < 2) return; relaxed = true; }   // 예비 후보
      }
      // 확장어는 상한(30)까지만 - 핵심 개념 일치가 순위를 정한다
      let score = coreScore + Math.min(relScore, 30) + coreHits * 6 + Math.min(relHits, 4) * 1.5;
      if (wantedRegion.length) {
        if (wantedRegion.some(w => it.t.includes(w) || it.o.includes(w))) score += 15;
        else if (hasOtherRegion(it.t, wantedRegion)) score -= 6;
      }
      score += Math.min(4, Math.pow(it.dl || 0, 0.35) / 6);
      if (it.cn) score += 2;
      const fmtText = ((it.fm || []).join(' ') + ' ' + it.t).toLowerCase();
      let mediaMatch = false;
      for (const m of modality) if ((MEDIA_WORDS[m] || []).some(w => fmtText.includes(w))) { mediaMatch = true; break; }
      const visual = ['이미지', '영상', '음성'].some(m => modality.has(m));
      if (it.s) {
        if (wantsAi) score = score * (mediaMatch ? 1.5 : 1.2) + 8; else score += 1.5;
      } else if (wantsAi && visual && !mediaMatch) {
        score *= 0.6;
      }
      if (isSemantic) score += 40;
      if (relaxed) fallback.push([score * 0.5, it, coreHits + relHits]);
      else scored.push([score, it, coreHits + relHits]);
    });
    scored.sort((a, b) => b[0] - a[0]);
    // 핵심 개념이 드문 낱말이라 후보가 너무 적으면 예비 후보로 채운다
    if (scored.length < 20 && fallback.length) {
      fallback.sort((a, b) => b[0] - a[0]);
      scored.push(...fallback.slice(0, Math.max(0, 40 - scored.length)));
    }

    // 다양성: 같은 데이터의 지역별 복제본은 묶음당 3건까지(질문 지역과 맞는 건 예외)
    const groupCount = new Map(), coarseCount = new Map();
    const quotaAi = Math.max(8, Math.floor(limit / 3)) * (wantsAi ? 2 : 1);
    const picked = []; let aiN = 0;
    for (const [score, it, covered] of scored) {
      if (picked.length >= limit) break;
      const key = titleGroupKey(it.t);
      const coarse = key.slice(0, 12);   // 앞부분만 같은 데이터(학교명만 바뀌는 설문 등)도 묶는다
      const isWanted = wantedRegion.length && wantedRegion.some(w => it.t.includes(w));
      if (!isWanted && (groupCount.get(key) || 0) >= 3) continue;
      if (!isWanted && (coarseCount.get(coarse) || 0) >= 5) continue;
      if (it.s) { if (aiN >= quotaAi && picked.length > limit * 0.6) continue; aiN++; }
      groupCount.set(key, (groupCount.get(key) || 0) + 1);
      coarseCount.set(coarse, (coarseCount.get(coarse) || 0) + 1);
      picked.push({ ...toRow(it), _score: Math.round(score * 10) / 10, _covered: covered, columns_text: it.mt });
    }
    return {
      candidates: picked,
      stats: { matched: totalRows, scanned_pool: chosen.length, keywords: kws, core, region: wantedRegion,
               modality: [...modality], wants_ai: wantsAi, engine: 'browser',
               keyword_hits: Object.fromEntries(kws.map((k, i) => [k, hits[i].size])), capped: false },
    };
  }
  window.STATIC_SEARCH = searchCandidates;

  // ---------------------------------------------------------------- 라우팅
  async function handleGet(path) {
    const url = new URL(path, location.origin);
    const p = url.pathname, params = url.searchParams;
    if (p === '/api/catalog/stats') {
      const m = await meta();
      return { database: true, total: m.total, sources: m.sources, aihub_access: m.aihub_access,
               collected: m.collected, meta: m.meta };
    }
    if (p === '/api/catalog/list') return catalogList(params);
    if (p === '/api/catalog/facets') return catalogFacets(params);
    if (p === '/api/catalog/detail') return catalogDetail(params);
    if (p === '/api/my') return readLS(LS_MY, { updated_at: null, items: {} });
    if (p === '/api/recommendations') return readLS(LS_RECO, { updated_at: null, items: [] });
    return null;   // 그 외(/api/settings, /api/ai/*)는 Functions 가 처리
  }

  async function handlePost(path, body) {
    const p = new URL(path, location.origin).pathname;
    if (p === '/api/my') {
      const data = { updated_at: new Date().toISOString().slice(0, 19).replace('T', ' '),
                     items: (body && body.items) || {} };
      writeLS(LS_MY, data);
      return { ok: true, updated_at: data.updated_at, count: Object.keys(data.items).length };
    }
    if (p === '/api/recommendations/delete') {
      const data = readLS(LS_RECO, { items: [] });
      data.items = (data.items || []).filter(x => x.id !== (body && body.id));
      writeLS(LS_RECO, data);
      return data;
    }
    if (p === '/api/ai/recommend') {
      const query = (body && body.query) || '';
      // 1) 브라우저에서 후보를 찾고 2) 서버(Functions)가 Gemini 를 호출한다.
      emit({ phase: 'search' });
      const plan = await callFunction('/api/ai/keywords', { query });
      const nonTabular = (plan.modality || []).some(m => m !== '정형');
      if ((truthy(plan.wants_ai_training) || nonTabular) && !plan.aihub_picks) {
        try {
          const titles = await getJSON('aihub-titles.json');
          const picked = await callFunction('/api/ai/aihub-pick', { query, core: plan.core, modality: plan.modality, titles });
          plan.aihub_picks = picked.picks || [];
          plan.aihub_pick_reasons = picked.reasons || {};
          plan.pick_usage = picked.cached ? null : (picked.usage || null);
        } catch (e) { plan.aihub_picks = []; plan.aihub_pick_reasons = {}; }
      }
      const found = await searchCandidates(plan, 120);
      if (!found.candidates.length) throw new Error('카탈로그에서 관련 데이터를 찾지 못했습니다. 다른 표현으로 검색해 보세요.');
      emit({ phase: 'ai' });
      const result = await callFunction('/api/ai/recommend', {
        query, keywords: plan.keywords, goal: plan.goal,
        plan: { core: plan.core, region: plan.region, modality: plan.modality, wants_ai_training: plan.wants_ai_training,
                aihub_picks: plan.aihub_picks || [], aihub_pick_reasons: plan.aihub_pick_reasons || {} },
        candidates: found.candidates.map(c => ({
          uid: c.uid, source: c.source, source_id: c.source_id, title: c.title, field: c.field,
          subfield: c.subfield, organization: c.organization, formats: c.formats,
          row_count: c.row_count, modified_at: c.modified_at, description: c.description,
          access: c.access, columns_text: c.columns_text, url: c.url, downloads: c.downloads,
          column_count: c.column_count,
        })),
      });
      result.search = Object.assign({}, found.stats, { cached: !!plan.cached });
      // 키워드 분석·의미 선별 호출의 토큰도 합산해 실제 사용량을 보여 준다
      const extra = [plan.cached ? null : plan.usage, plan.pick_usage].filter(Boolean);
      if (result.usage && extra.length) {
        extra.forEach(u => { ['prompt', 'output', 'thoughts', 'total'].forEach(k => { result.usage[k] = (result.usage[k] || 0) + (u[k] || 0); }); });
        result.usage.calls = (result.usage.calls || 0) + extra.length - (plan.cached ? 0 : 1);
        if (result.usage.cost) {
          const rin = result.usage.cost.rate_in || 0.3, rout = result.usage.cost.rate_out || 2.5;
          const usd = (result.usage.prompt / 1e6) * rin + ((result.usage.output + result.usage.thoughts) / 1e6) * rout;
          result.usage.cost.usd = Math.round(usd * 1e6) / 1e6;
          result.usage.cost.krw = Math.round(usd * 1400 * 10) / 10;
        }
      }
      const store = readLS(LS_RECO, { items: [] });
      store.items = [result].concat((store.items || []).filter(x => x.id !== result.id)).slice(0, 200);
      store.updated_at = new Date().toISOString().slice(0, 19).replace('T', ' ');
      writeLS(LS_RECO, store);
      return result;
    }
    return null;
  }

  async function callFunction(path, body) {
    const r = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}), credentials: 'same-origin',
    });
    let data = {};
    try { data = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(data.error || ('요청 실패 (' + r.status + ')'));
    return data;
  }

  window.STATIC_API = handleGet;
  window.STATIC_POST = handlePost;
  window.STATIC_MODE = true;
})();
