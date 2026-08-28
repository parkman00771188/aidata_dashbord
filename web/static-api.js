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

  /** ai_service.search_candidates 의 브라우저 판(같은 가중치·IDF·키워드별 확보). */
  async function searchCandidates(keywords, limit) {
    await loadIndex();
    limit = limit || 60;
    const kws = [];
    (keywords || []).forEach(k => {
      k = String(k || '').trim();
      if (k.length < 2 || STOPWORDS.has(k) || kws.indexOf(k) >= 0) return;
      kws.push(k);
      const ns = k.replace(/\s+/g, '');
      if (ns !== k && ns.length >= 2 && kws.indexOf(ns) < 0) kws.push(ns);
    });
    if (!kws.length) return { candidates: [], stats: null };
    const low = kws.map(k => k.toLowerCase());

    const hits = low.map(() => new Map());
    INDEX.forEach(it => {
      for (let i = 0; i < low.length; i++) {
        const kw = low[i];
        let s = 0;
        if (it._title.indexOf(kw) >= 0) s += 6;
        if (it._meta.indexOf(kw) >= 0) s += 3;
        if (s === 0 && it._hay.indexOf(kw) >= 0) s += 1.5;
        if (s) hits[i].set(it.uid, s);
      }
    });
    const totalRows = Math.max(1, new Set([].concat.apply([], hits.map(h => [...h.keys()]))).size);
    const idf = hits.map(h => Math.max(0.35, Math.log((totalRows + 1) / (h.size + 1)) / Math.log(20) + 0.35));

    const combined = new Map();
    hits.forEach((h, i) => h.forEach((raw, uid) => {
      const slot = combined.get(uid) || [0, 0];
      slot[0] += raw * idf[i]; slot[1] += 1;
      combined.set(uid, slot);
    }));
    const byUid = new Map(INDEX.map(it => [it.uid, it]));
    const ranked = [...combined.entries()].sort((a, b) =>
      (b[1][0] + b[1][1] * 3) - (a[1][0] + a[1][1] * 3));

    const chosen = [], seen = new Set();
    ranked.slice(0, 220).forEach(([uid]) => { seen.add(uid); chosen.push(uid); });
    hits.forEach(h => [...h.entries()].sort((a, b) => b[1] - a[1]).slice(0, 30)
      .forEach(([uid]) => { if (!seen.has(uid)) { seen.add(uid); chosen.push(uid); } }));

    const scored = [];
    chosen.forEach(uid => {
      const it = byUid.get(uid); if (!it) return;
      const [base, covered] = combined.get(uid) || [0, 0];
      if (!covered) return;
      let score = base + covered * 4;
      score += Math.min(4, Math.pow(it.dl || 0, 0.35) / 6);
      if (it.cn) score += 2;
      if (it.s) score += 1.5;
      scored.push([score, it, covered]);
    });
    scored.sort((a, b) => b[0] - a[0]);
    const quotaAi = Math.max(8, Math.floor(limit / 3));
    const picked = []; let aiN = 0;
    for (const [score, it, covered] of scored) {
      if (picked.length >= limit) break;
      if (it.s) { if (aiN >= quotaAi && picked.length > limit * 0.6) continue; aiN++; }
      picked.push({ ...toRow(it), _score: Math.round(score * 10) / 10, _covered: covered, columns_text: it.mt });
    }
    return {
      candidates: picked,
      stats: { matched: totalRows, scanned_pool: chosen.length, keywords: kws, engine: 'browser',
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
      const found = await searchCandidates(plan.keywords || [], 60);
      if (!found.candidates.length) throw new Error('카탈로그에서 관련 데이터를 찾지 못했습니다. 다른 표현으로 검색해 보세요.');
      emit({ phase: 'ai' });
      const result = await callFunction('/api/ai/recommend', {
        query, keywords: plan.keywords, goal: plan.goal,
        candidates: found.candidates.map(c => ({
          uid: c.uid, source: c.source, source_id: c.source_id, title: c.title, field: c.field,
          subfield: c.subfield, organization: c.organization, formats: c.formats,
          row_count: c.row_count, modified_at: c.modified_at, description: c.description,
          access: c.access, columns_text: c.columns_text, url: c.url, downloads: c.downloads,
          column_count: c.column_count,
        })),
      });
      result.search = Object.assign({}, found.stats, { cached: !!plan.cached });
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
