# -*- coding: utf-8 -*-
"""
AI-Hub 데이터셋 목록/상세 크롤러
  python crawl_aihub.py            # 목록 + 상세 수집(캐시 사용) + 파싱 → data/datasets.json + data/details/<sn>.json
  python crawl_aihub.py --refresh  # 목록 다시 받고, 새 데이터셋만 상세 추가 수집
  python crawl_aihub.py --parse    # 재수집 없이 raw/ 캐시만 다시 파싱
"""
import os, re, sys, json, time, argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup, Comment

BASE = 'https://aihub.or.kr'
LIST_URL = (BASE + '/aihubdata/data/list.do?pageIndex={page}&currMenu=115&topMenu=100'
            '&srchOptnCnd=OPTNCND001&searchKeyword=&srchDetailCnd=DETAILCND001&srchOrder=ORDER001&srchPagePer=80')
VIEW_URL = BASE + '/aihubdata/data/view.do?currMenu=115&topMenu=100&aihubDataSe=data&dataSetSn={sn}'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36',
           'Accept-Language': 'ko-KR,ko;q=0.9'}
ROOT = os.path.dirname(os.path.abspath(__file__))
RAW_LIST = os.path.join(ROOT, 'raw', 'list')
RAW_VIEW = os.path.join(ROOT, 'raw', 'view')
OUT_DIR = os.path.join(ROOT, 'data')
for d in (RAW_LIST, RAW_VIEW, OUT_DIR):
    os.makedirs(d, exist_ok=True)

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url, retries=3):
    last = None
    for i in range(retries):
        try:
            r = session.get(url, timeout=40)
            if r.status_code == 200 and len(r.text) > 5000:
                return r.text
            last = 'status %s len %s' % (r.status_code, len(r.text))
        except Exception as e:  # noqa
            last = repr(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError('fetch failed: %s (%s)' % (url, last))


def clean(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def to_int(s):
    s = re.sub(r'[^\d]', '', s or '')
    return int(s) if s else 0


# ---------------------------------------------------------------- 목록
def crawl_list(refresh=False):
    items, page = {}, 1
    total = None
    while True:
        path = os.path.join(RAW_LIST, 'page_%d.html' % page)
        if os.path.exists(path) and not refresh:
            html = open(path, encoding='utf-8').read()
        else:
            html = fetch(LIST_URL.format(page=page))
            open(path, 'w', encoding='utf-8').write(html)
            time.sleep(0.5)
        s = BeautifulSoup(html, 'html.parser')
        if total is None:
            m = re.search(r'\((\d[\d,]*)건\)', s.get_text())
            total = to_int(m.group(1)) if m else None
        lis = s.select('ul#dataResultOL > li')
        if not lis:
            break
        for li in lis:
            a = li.find('a', href=re.compile(r'dataSetSn=\d+'))
            if not a:
                continue
            sn = re.search(r'dataSetSn=(\d+)', a['href']).group(1)
            vol = li.select_one('.fileVolume')
            info = {}
            for p in li.select('.info > p'):
                sp = p.select_one('span')
                if sp and sp.get('class'):
                    info[sp['class'][0]] = to_int(p.get_text())
            items[sn] = {
                'sn': sn,
                'title': clean(li.select_one('.textBox .text').get_text()) if li.select_one('.textBox .text') else '',
                'kind': clean(li.select_one('.kind').get_text()) if li.select_one('.kind') else '',
                'thumb': (BASE + li.img['src']) if li.img and li.img.get('src') else '',
                'labels': [clean(x.get_text()) for x in li.select('.labelPos')],
                'views': info.get('view', 0), 'likes': info.get('like', 0), 'downloads': info.get('down', 0),
                'size_bytes': to_int(vol.get_text()) if vol else 0,
                'build_year_list': vol.get('data-datacnstcyear', '') if vol else '',
                'list_order': len(items),
            }
        print('[list] page %d: %d items (total %d/%s)' % (page, len(lis), len(items), total), flush=True)
        if len(lis) < 80:
            break
        page += 1
    return items, total


# ---------------------------------------------------------------- 상세 수집
def crawl_view(sn, force=False):
    path = os.path.join(RAW_VIEW, '%s.html' % sn)
    if os.path.exists(path) and not force:
        return sn, 'cached'
    html = fetch(VIEW_URL.format(sn=sn))
    open(path, 'w', encoding='utf-8').write(html)
    return sn, 'fetched'


def crawl_views(sns, workers=6):
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(crawl_view, sn): sn for sn in sns}
        for f in as_completed(futs):
            done += 1
            try:
                sn, st = f.result()
                if done % 25 == 0:
                    print('[view] %d/%d (%s %s)' % (done, len(sns), sn, st), flush=True)
            except Exception as e:
                print('[view] ERROR %s: %s' % (futs[f], e), flush=True)


# ---------------------------------------------------------------- HTML 정리(대시보드 삽입용)
KEEP_TAGS = {'h4', 'h5', 'h6', 'p', 'table', 'caption', 'thead', 'tbody', 'tr', 'th', 'td', 'pre',
             'ul', 'ol', 'li', 'br', 'strong', 'em', 'b', 'span', 'div'}
KEEP_ATTRS = {'scope', 'colspan', 'rowspan'}


def sanitize(node, max_len=60000):
    """섹션 HTML을 태그/속성 최소화해서 문자열로. 이미지/링크/스크립트 제거."""
    if node is None:
        return ''
    node = BeautifulSoup(str(node), 'html.parser')
    for c in node.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    for t in node.find_all(['script', 'style', 'img', 'iframe', 'video', 'button', 'input', 'form']):
        t.decompose()
    for t in node.find_all(True):
        if t.name == 'a' or t.name not in KEEP_TAGS:
            t.unwrap()
            continue
        for a in list(t.attrs):
            if a not in KEEP_ATTRS:
                del t.attrs[a]
    for t in node.find_all(string=True):
        if t.parent is not None and t.parent.name != 'pre':
            new = re.sub(r'\s+', ' ', t)
            if new != t:
                t.replace_with(new)
    html = str(node)
    html = re.sub(r'>\s+<', '><', html)
    if len(html) > max_len:
        html = html[:max_len] + '<p><em>… (내용이 길어 일부 생략. 원문은 AI-Hub 페이지 참조)</em></p>'
    return html


def table_rows(tbl):
    rows = []
    for tr in tbl.find_all('tr'):
        cells = [clean(c.get_text(' ')) for c in tr.find_all(['th', 'td'])]
        if cells:
            rows.append(cells)
    return rows


# ---------------------------------------------------------------- 상세 파싱
def parse_view(sn, base):
    path = os.path.join(RAW_VIEW, '%s.html' % sn)
    if not os.path.exists(path):
        return None
    html = open(path, encoding='utf-8').read()
    s = BeautifulSoup(html, 'html.parser')
    d = dict(base)
    d['url'] = VIEW_URL.format(sn=sn)

    head = s.select_one('.dataHead')
    if head is None:
        d['parse_error'] = 'no dataHead'
        d['status'] = '파싱오류'
        return d
    h3 = head.find('h3')
    if h3:
        for lab in h3.select('.labelPos'):
            lab.extract()
        d['title'] = clean(h3.get_text()) or d.get('title', '')
    d['tags'] = [clean(a.get_text()).lstrip('#') for a in head.select('.tag a')]
    d['field'] = d.get('kind', '')
    d['types'] = []
    d['gen_method'] = ''
    for li in head.select('.content > ul > li'):
        em = li.find('em')
        if not em:
            continue
        key = clean(em.get_text())
        em.extract()
        val = clean(li.get_text(' '))
        val = re.sub(r'\s*,\s*', ', ', val)
        if key == '분야':
            d['field'] = val
        elif key == '유형':
            d['types'] = [x for x in (t.strip() for t in val.split(',')) if x]
        elif key.replace(' ', '') == '생성방식':
            d['gen_method'] = val
    d['build_year'] = d.get('build_year_list', '')
    d['update_ym'] = ''
    for em in head.select('.date em'):
        t = clean(em.get_text(' '))
        if t.startswith('구축년도'):
            d['build_year'] = t.split(':', 1)[-1].strip()
        elif t.startswith('갱신년월'):
            d['update_ym'] = t.split(':', 1)[-1].strip()
        elif t.startswith('조회수'):
            d['views'] = to_int(t.split(':', 1)[-1]) or d.get('views', 0)
        elif t.startswith('다운로드'):
            d['downloads'] = to_int(t.split(':', 1)[-1]) or d.get('downloads', 0)
    like = head.select_one('.function .like')
    if like:
        d['likes'] = to_int(like.get_text()) or d.get('likes', 0)

    # ---- 다운로드 가능 여부
    m = re.search(r"var s3FileSize\s*=\s*'(\d*)'", html)
    if m and m.group(1):
        d['size_bytes'] = int(m.group(1))
    m = re.search(r"var s3FileCnt\s*=\s*'(\d*)'", html)
    d['s3_file_cnt'] = int(m.group(1)) if m and m.group(1) else 0
    m = re.search(r"var indvdlzConfmCode\s*=\s*'([^']*)'", html)
    d['indvdlz_code'] = m.group(1) if m else ''
    btn_texts = [clean(b.get_text()) for b in head.select('.function .button button, .function .button a.button')]
    d['head_buttons'] = btn_texts
    d['has_download_btn'] = any('fnDwldReqst' in (b.get('onclick') or '') for b in head.select('.function button'))
    d['has_sample'] = any('샘플' in t for t in btn_texts)
    d['tabs'] = [clean(a.get_text()) for a in s.select('.linkList.dataHeadLink a')]
    d['is_safezone'] = ('이용신청' in d['tabs']) or ('안심존' in ' '.join(btn_texts))
    d['healthcare_notice'] = bool(s.find(string=re.compile('안심존을 통해 개방')))
    d['has_api_filelist'] = any('파일 목록' in t for t in d['tabs'])
    d['approval_required'] = ('신청하기' in btn_texts) or d['indvdlz_code'] in ('PROCSS003', 'PROCSS004')
    d['offline_available'] = '오프라인 이용' in d['tabs']
    notice = s.select_one('.dataNotice')
    d['notice'] = clean(notice.get_text()) if notice else ''
    if d['has_download_btn'] and (d['s3_file_cnt'] > 0 or d['indvdlz_code'] == 'PROCSS004'):
        d['status'] = '데이터 있음'
    elif d['has_download_btn']:
        d['status'] = '준비중'
    elif d['is_safezone']:
        d['status'] = '안심존'
    else:
        d['status'] = '다운로드 없음'

    # ---- 아코디언 섹션
    sec = {}
    for li in s.select('ul.fold.dataContent > li'):
        sec[li.get('data-accordion-key')] = li.find('div', recursive=False)
    d['sections_available'] = [k for k, v in sec.items() if v is not None and clean(v.get_text())]
    content = sec.get('content')
    d['intro'] = ''
    d['purpose'] = ''
    d['version_history'] = []
    d['data_history'] = []
    if content is not None:
        for h4 in content.find_all('h4'):
            name = clean(h4.get_text())
            nxt = h4.find_next_sibling()
            if nxt is None:
                continue
            if name in ('소개', '구축목적'):
                # h4 다음 ~ 다음 h4 전까지의 모든 형제 요소를 합침(구 데이터는 pre/p 여러 개로 분리됨)
                parts, cur = [], nxt
                while cur is not None and cur.name != 'h4':
                    parts.append(clean(cur.get_text(' ')))
                    cur = cur.find_next_sibling()
                d['intro' if name == '소개' else 'purpose'] = clean(' '.join(x for x in parts if x))
            elif name == '데이터 변경이력' and nxt.name == 'table':
                d['version_history'] = table_rows(nxt)[1:]
            elif name == '데이터 히스토리' and nxt.name == 'table':
                d['data_history'] = table_rows(nxt)[1:]
    meta = {}
    if sec.get('meta') is not None:
        for tr in sec['meta'].find_all('tr'):
            cells = tr.find_all(['th', 'td'])
            for i in range(0, len(cells) - 1, 2):
                if cells[i].name == 'th':
                    k = clean(cells[i].get_text(' ')).replace(' /', '/').replace('/ ', '/')
                    v = clean(cells[i + 1].get_text(' '))
                    if k == '데이터 유형':
                        v = re.sub(r'\s*,\s*', ', ', v)
                    if k:
                        meta[k] = v
    if not meta and d['intro']:
        # 2017~2020 데이터: 소개 문단 안에 "- 데이터 영역 : xxx - 데이터 유형 : yyy" 형태로 기재됨
        for m2 in re.finditer(r'-\s*(데이터 영역|데이터 유형|구축년도|구축량|데이터 형식|라벨링 유형|라벨링 형식|데이터 출처)\s*:\s*(.+?)(?=\s+-\s*[가-힣 ]{2,8}\s*:|$)', d['intro']):
            meta[m2.group(1)] = clean(m2.group(2))
        if meta:
            d['intro'] = clean(re.split(r'\s+-\s*(?:데이터 영역|데이터 유형|구축년도|구축량)\s*:', d['intro'])[0])
    d['meta'] = meta
    d['data_format'] = meta.get('데이터 형식', '')
    d['label_type'] = meta.get('라벨링 유형', '')
    d['label_format'] = meta.get('라벨링 형식', '')
    d['build_amount'] = meta.get('데이터 구축년도/데이터 구축량', '') or meta.get('데이터 구축량', '') or meta.get('구축량', '')
    d['data_source'] = meta.get('데이터 출처', '')
    d['use_service'] = meta.get('데이터 활용 서비스', '')

    # 구축 업체
    d['builder_main'] = ''
    d['builder_sub'] = []
    c = sec.get('cnstcEntrps')
    if c is not None:
        h5 = c.find('h5', string=re.compile('주관'))
        if h5:
            d['builder_main'] = clean(h5.get_text()).split(':', 1)[-1].strip()
        for tbl in c.find_all('table'):
            cap = tbl.find('caption')
            if cap and '참여' in cap.get_text():
                d['builder_sub'] = [r[0] for r in table_rows(tbl)[1:] if r]
    # AI 모델 성능 요약
    d['ai_perf'] = []
    p = sec.get('prcuseAi')
    if p is not None:
        for tbl in p.find_all('table'):
            hdr = clean(tbl.get_text(' '))[:200]
            if 'AI Task' in hdr or '성능지표' in hdr:
                d['ai_perf'] = table_rows(tbl)
                break
    txt = s.get_text(' ')
    d['has_manual'] = '데이터 설명서 다운로드' in txt
    d['has_guide'] = '구축활용가이드 다운로드' in txt

    d['sections'] = {
        'content': sanitize(content),
        'meta': sanitize(sec.get('meta')),
        'stats': sanitize(sec.get('stats')),
        'annotation': sanitize(sec.get('annotation'), 40000),
        'prcuseAi': sanitize(sec.get('prcuseAi'), 30000),
        'prfomnc': sanitize(sec.get('prfomnc'), 30000),
        'autool': sanitize(sec.get('autool'), 8000),
        'cnstcEntrps': sanitize(sec.get('cnstcEntrps')),
    }
    return d


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--refresh', action='store_true', help='목록 페이지 다시 받기')
    ap.add_argument('--parse', action='store_true', help='수집 없이 파싱만')
    ap.add_argument('--workers', type=int, default=6)
    args = ap.parse_args()

    items, total = crawl_list(refresh=args.refresh)
    print('[list] %d datasets (site says %s)' % (len(items), total), flush=True)
    if not args.parse:
        crawl_views(list(items), workers=args.workers)

    out, errors = [], []
    for sn, base in items.items():
        try:
            d = parse_view(sn, base)
            if d is None:
                errors.append((sn, 'no raw html'))
                d = dict(base, url=VIEW_URL.format(sn=sn), status='미수집')
            out.append(d)
        except Exception as e:
            errors.append((sn, repr(e)))
            out.append(dict(base, url=VIEW_URL.format(sn=sn), status='파싱오류', parse_error=repr(e)))
    out.sort(key=lambda x: x['list_order'])
    meta = {'crawled_at': time.strftime('%Y-%m-%d %H:%M'), 'total_site': total, 'count': len(out),
            'errors': errors}
    # 상세(무거운 HTML 섹션 등)는 data/details/<sn>.json 으로 분리, 목록용 JSON은 경량화
    DETAIL_KEYS = ('sections', 'version_history', 'data_history', 'ai_perf', 'head_buttons', 'tabs', 'sections_available')
    det_dir = os.path.join(OUT_DIR, 'details')
    os.makedirs(det_dir, exist_ok=True)
    light = []
    for d in out:
        det = {k: d[k] for k in DETAIL_KEYS if k in d}
        det['sn'] = d['sn']
        json.dump(det, open(os.path.join(det_dir, '%s.json' % d['sn']), 'w', encoding='utf-8'), ensure_ascii=False)
        light.append({k: v for k, v in d.items() if k not in DETAIL_KEYS})
    payload = {'meta': meta, 'datasets': light}
    json.dump(payload, open(os.path.join(OUT_DIR, 'datasets.json'), 'w', encoding='utf-8'), ensure_ascii=False)
    legacy = os.path.join(OUT_DIR, 'datasets.js')
    if os.path.exists(legacy):
        os.remove(legacy)
    print('[done] %d datasets; errors: %d' % (meta['count'], len(errors)))
    print('[status]', Counter(d.get('status') for d in out))
    print('[field]', Counter(d.get('field') for d in out).most_common(30))


if __name__ == '__main__':
    main()
