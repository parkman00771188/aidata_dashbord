# -*- coding: utf-8 -*-
"""AI Hub + 공공데이터포털 통합 로컬 대시보드 서버."""
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import ai_service
from catalog_db import (DB_PATH, build_column_index, build_search_index, connect, decode_json,
                        import_aihub, init_db, row_summary)

ROOT = os.path.dirname(os.path.abspath(__file__))
MY_PATH = os.path.join(ROOT, 'data', 'my_datasets.json')
AIHUB_JSON = os.path.join(ROOT, 'data', 'datasets.json')
PORT = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8765)
LOCK = threading.Lock()
SOURCE_MAP = {'aihub': 'AI Hub', 'public': '공공데이터포털'}
SORT_COLUMNS = {
    'list_order': 'list_order', 'title': 'title', 'field': 'field',
    'organization': 'organization', 'modified_at': 'modified_at',
    'row_count': 'row_count', 'views': 'views', 'downloads': 'downloads',
}
LIST_COLUMNS = (
    'uid,source,source_id,list_order,title,file_name,field,subfield,organization,'
    'organization_type,formats_json,keywords_json,description,modified_at,created_at,next_update,'
    'update_cycle,media_type,row_count,views,downloads,url,detail_status,preview_status,crawled_at,'
    "json_extract(detail_json,'$.delivery') delivery,json_extract(detail_json,'$.extension') extension"
)

# AI Hub 제공방식(다운로드 / 안심존)은 data/datasets.json 에만 있으므로 메모리에 올려 쓴다.
AIHUB_ACCESS = {}
AIHUB_ACCESS_MTIME = 0.0


def load_aihub_access(force=False):
    """data/datasets.json 에서 sn별 제공방식/용량을 읽어 캐시한다."""
    global AIHUB_ACCESS, AIHUB_ACCESS_MTIME
    try:
        mtime = os.path.getmtime(AIHUB_JSON)
    except OSError:
        return AIHUB_ACCESS
    if not force and mtime == AIHUB_ACCESS_MTIME and AIHUB_ACCESS:
        return AIHUB_ACCESS
    try:
        with open(AIHUB_JSON, encoding='utf-8') as f:
            payload = json.load(f)
    except Exception:
        return AIHUB_ACCESS
    table = {}
    for item in payload.get('datasets', []):
        sn = str(item.get('sn') or '')
        if not sn:
            continue
        table[sn] = {
            'status': item.get('status') or '',
            'approval_required': bool(item.get('approval_required')),
            'offline_available': bool(item.get('offline_available')),
            'has_sample': bool(item.get('has_sample')),
            'size_bytes': int(item.get('size_bytes') or 0),
            's3_file_cnt': int(item.get('s3_file_cnt') or 0),
        }
    AIHUB_ACCESS, AIHUB_ACCESS_MTIME = table, mtime
    return AIHUB_ACCESS


def prepare_catalog():
    # 새로 clone 한 경우 catalog.db 가 없으므로 깃에 포함된 스냅샷에서 복원한다.
    if not os.path.exists(DB_PATH):
        try:
            import snapshot
            if os.path.exists(snapshot.MANIFEST):
                print('catalog.db 가 없어 스냅샷에서 복원합니다…', flush=True)
                snapshot.restore()
        except Exception as e:  # noqa
            print('스냅샷 복원 실패: %r' % e, flush=True)
    # 수집기가 DB에 쓰는 중이면 잠금이 걸릴 수 있다. 동기화는 필요할 때만 시도하고,
    # 실패하더라도 조회 기능은 그대로 동작하므로 서버를 계속 띄운다.
    try:
        con = connect(DB_PATH)
        try:
            init_db(con)
            if aihub_sync_needed(con):
                import_aihub(con)
            n = build_column_index(con)  # 비어 있을 때만 만든다
            if n:
                print('데이터 항목 색인 %d건 생성' % n, flush=True)
            n = build_search_index(con, rebuild=bool(n))
            if n:
                print('검색 색인 %d건 생성' % n, flush=True)
        finally:
            con.close()
    except Exception as e:  # noqa
        print('AI Hub 동기화를 건너뜁니다(%s). 수집기가 DB를 쓰는 중일 수 있으며 조회는 정상 동작합니다.'
              % e.__class__.__name__, flush=True)
    load_aihub_access(force=True)


def aihub_sync_needed(con):
    """data/datasets.json 이 새로 수집됐을 때만 카탈로그에 다시 넣는다."""
    try:
        with open(AIHUB_JSON, encoding='utf-8') as f:
            stamp = (json.load(f).get('meta') or {}).get('crawled_at', '')
    except Exception:
        return False
    row = con.execute("SELECT value FROM crawl_meta WHERE key='aihub_crawled_at'").fetchone()
    have = (row[0] if row else '') or ''
    count = con.execute("SELECT COUNT(*) FROM catalog_items WHERE source='AI Hub' AND active=1").fetchone()[0]
    return count == 0 or have.strip('"') != stamp


# 분야·형식 집계와 전체 통계는 매 페이지 로드마다 같은 값이라 캐시한다.
# (DB 파일이 바뀌면 자동으로 무효화된다.)
_AGG_CACHE = {}


def cached_agg(key, build):
    try:
        stamp = os.path.getmtime(DB_PATH)
    except OSError:
        stamp = 0
    hit = _AGG_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    value = build()
    if len(_AGG_CACHE) > 40:
        _AGG_CACHE.clear()
    _AGG_CACHE[key] = (stamp, value)
    return value


def one(query, key, default=''):
    values = query.get(key)
    return values[0] if values else default


def many(query, key, limit=40):
    """field=A&field=B 또는 field=A,B 모두 허용."""
    out = []
    for raw in query.get(key, []):
        for piece in str(raw).split(','):
            piece = piece.strip()
            if piece and piece not in out:
                out.append(piece)
    return out[:limit]


def attach_access(row):
    row['access'] = ai_service.access_of(row, load_aihub_access())
    if row.get('source') == 'AI Hub':
        info = load_aihub_access().get(str(row.get('source_id') or '')) or {}
        row['size_bytes'] = info.get('size_bytes', 0)
        row['has_sample'] = info.get('has_sample', False)
        row['offline_available'] = info.get('offline_available', False)
    row.pop('delivery', None)
    row.pop('extension', None)
    return row


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):  # API 호출만 로그
        try:
            msg = fmt % args
        except Exception:
            msg = str(fmt)
        if '/api/' in msg:
            sys.stderr.write('%s %s\n' % (time.strftime('%H:%M:%S'), msg))

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def end_headers(self):
        if self.path.endswith('.json') or self.path.endswith('.html') or self.path == '/':
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == '/api/my':
            with LOCK:
                if os.path.exists(MY_PATH):
                    try:
                        data = json.load(open(MY_PATH, encoding='utf-8'))
                    except Exception as e:
                        return self._json(500, {'error': repr(e)})
                else:
                    data = {'updated_at': None, 'items': {}}
            return self._json(200, data)
        if path == '/api/catalog/stats':
            return self.catalog_stats(query)
        if path == '/api/catalog/list':
            return self.catalog_list(query)
        if path == '/api/catalog/detail':
            return self.catalog_detail(query)
        if path == '/api/catalog/facets':
            return self.catalog_facets(query)
        if path == '/api/settings':
            return self._json(200, ai_service.public_settings())
        if path == '/api/ai/models':
            pub = ai_service.public_settings()
            try:
                models = ai_service.list_models()
                live = pub['has_key'] and models is not ai_service.FALLBACK_MODELS
                return self._json(200, {'models': models, 'live': live, 'current': pub['model']})
            except ai_service.AiError as e:
                return self._json(200, {'models': ai_service.FALLBACK_MODELS, 'live': False,
                                        'error': str(e), 'current': pub['model']})
        if path == '/api/recommendations':
            return self._json(200, ai_service.load_recos())
        if path == '/':
            self.path = '/catalog.html'
        return super().do_GET()

    # ------------------------------------------------------------------ 카탈로그
    def _catalog_where(self, query, include_search=True, include_field=True):
        where, args = ['active=1'], []
        source = one(query, 'source')
        if source in SOURCE_MAP:
            where.append('source=?')
            args.append(SOURCE_MAP[source])
        fields = many(query, 'field')
        if fields and include_field:
            where.append('field IN (%s)' % ','.join('?' * len(fields)))
            args.extend(fields)
        organization = one(query, 'organization')
        if organization:
            where.append('organization=?')
            args.append(organization)
        formats = many(query, 'format', 12)
        if formats:
            where.append('EXISTS (SELECT 1 FROM item_formats f WHERE f.uid=catalog_items.uid AND f.format IN (%s))'
                         % ','.join('?' * len(formats)))
            args.extend([f.upper() for f in formats])
        preview = one(query, 'preview')
        if preview == 'yes':
            where.append("preview_status='ok'")
        elif preview == 'no':
            where.append("preview_status NOT IN ('ok','not_applicable')")
        if include_search:
            terms = [x for x in one(query, 'q').split() if x][:8]
            for term in terms:
                like = '%' + term + '%'
                where.append('(title LIKE ? OR file_name LIKE ? OR organization LIKE ? OR keywords_json LIKE ? OR description LIKE ?)')
                args.extend([like] * 5)
        return ' AND '.join(where), args

    def catalog_stats(self, query):
        if not os.path.exists(DB_PATH):
            return self._json(200, {'total': 0, 'sources': [], 'meta': {}, 'database': False})
        return self._json(200, cached_agg(('stats',), self._stats))

    def _stats(self):
        con = connect(DB_PATH, readonly=True)
        try:
            rows = con.execute(
                "SELECT source,COUNT(*) count,"
                "SUM(CASE WHEN detail_status IN ('bulk','ok') THEN 1 ELSE 0 END) details,"
                "SUM(CASE WHEN preview_status IN ('ok','none','not_applicable') THEN 1 ELSE 0 END) previews "
                "FROM catalog_items WHERE active=1 GROUP BY source ORDER BY source"
            ).fetchall()
            sources = [dict(r) for r in rows]
            meta = {r['key']: r['value'] for r in con.execute('SELECT key,value FROM crawl_meta')}
            for key, value in list(meta.items()):
                try:
                    meta[key] = json.loads(value)
                except (TypeError, ValueError):
                    pass
            access = {'다운로드': 0, '안심존': 0}
            for info in load_aihub_access().values():
                if info.get('status') == '안심존':
                    access['안심존'] += 1
                elif info.get('status') == '데이터 있음':
                    access['다운로드'] += 1
            # 화면에 쓸 수집 시각. 목록만 보여주면 미리보기 수집이 끝난 시점이
            # 반영되지 않아 데이터가 오래된 것처럼 보인다.
            def clean_stamp(value):
                return str(value or '').strip().strip('"')[:19]

            collected = {
                'aihub': clean_stamp(meta.get('aihub_crawled_at')),
                'public_list': clean_stamp(meta.get('public_list_crawled_at')),
                'public_preview': clean_stamp(meta.get('public_preview_last_run')),
            }
            row = con.execute('SELECT MAX(crawled_at) FROM catalog_items WHERE active=1').fetchone()
            collected['last_item'] = clean_stamp(row[0] if row else '')
            collected['latest'] = max([v for v in collected.values() if v] or [''])
            return {
                'database': True,
                'total': sum(r['count'] for r in sources),
                'sources': sources,
                'aihub_access': access,
                'collected': collected,
                'meta': meta,
            }
        finally:
            con.close()

    def catalog_list(self, query):
        if not os.path.exists(DB_PATH):
            return self._json(503, {'error': 'catalog database not found'})
        try:
            page = max(1, int(one(query, 'page', '1')))
            per = min(200, max(10, int(one(query, 'per', '50'))))
        except ValueError:
            return self._json(400, {'error': 'invalid page'})
        sort = SORT_COLUMNS.get(one(query, 'sort', 'modified_at'), 'modified_at')
        direction = 'ASC' if one(query, 'dir', 'desc').lower() == 'asc' else 'DESC'
        where, args = self._catalog_where(query)
        con = connect(DB_PATH, readonly=True)
        try:
            total = con.execute('SELECT COUNT(*) FROM catalog_items WHERE ' + where, args).fetchone()[0]
            select = ('SELECT ' + LIST_COLUMNS + ' FROM catalog_items WHERE ' + where +
                      ' ORDER BY ' + sort + ' ' + direction + ', uid ASC LIMIT ? OFFSET ?')
            rows = [attach_access(row_summary(r)) for r in con.execute(select, args + [per, (page - 1) * per])]
            return self._json(200, {'total': total, 'page': page, 'per': per, 'items': rows})
        finally:
            con.close()

    def catalog_detail(self, query):
        uid = one(query, 'uid')
        if not uid or ':' not in uid:
            return self._json(400, {'error': 'uid required'})
        con = connect(DB_PATH, readonly=True)
        try:
            row = con.execute('SELECT * FROM catalog_items WHERE uid=? AND active=1', (uid,)).fetchone()
            if row is None:
                return self._json(404, {'error': 'not found'})
            detail = decode_json(row['detail_json'], {})
            result = row_summary(row)
            result['delivery'] = detail.get('delivery', '')
            result['extension'] = detail.get('extension', '')
            attach_access(result)
            result['detail'] = detail
            result['preview'] = decode_json(row['preview_json'], {})
            result['error'] = row['error']
            cols = con.execute('SELECT columns_json,n FROM item_columns WHERE uid=?', (uid,)).fetchone()
            result['columns'] = decode_json(cols['columns_json'], []) if cols else []
            result['column_count'] = cols['n'] if cols else 0
            if row['source'] == 'AI Hub':
                detail_path = os.path.join(ROOT, 'data', 'details', row['source_id'] + '.json')
                if os.path.exists(detail_path):
                    try:
                        with open(detail_path, encoding='utf-8') as f:
                            result['detail'] = json.load(f)
                    except Exception as exc:
                        result['error'] = repr(exc)
                result['aihub'] = self.aihub_summary(row['source_id'])
            return self._json(200, result)
        finally:
            con.close()

    def aihub_summary(self, sn):
        """AI Hub 원본 목록(datasets.json)의 요약 필드를 그대로 전달한다."""
        try:
            with open(AIHUB_JSON, encoding='utf-8') as f:
                payload = json.load(f)
        except Exception:
            return {}
        for item in payload.get('datasets', []):
            if str(item.get('sn')) == str(sn):
                keep = ('intro', 'purpose', 'meta', 'build_year', 'update_ym', 'build_amount', 'data_format',
                        'label_type', 'label_format', 'data_source', 'use_service', 'builder_main', 'builder_sub',
                        'gen_method', 'types', 'tags', 'size_bytes', 'status', 'approval_required',
                        'offline_available', 'has_sample', 'has_manual', 'has_guide', 's3_file_cnt', 'notice')
                return {k: item.get(k) for k in keep if item.get(k) not in (None, '')}
        return {}

    def catalog_facets(self, query):
        key = ('facets', one(query, 'source'), tuple(many(query, 'format', 12)), one(query, 'preview'))
        return self._json(200, cached_agg(key, lambda: self._facets(query)))

    def _facets(self, query):
        con = connect(DB_PATH, readonly=True)
        try:
            # 분야 칩은 다중 선택이므로 분야 조건을 뺀 집합에서 개수를 센다.
            where, args = self._catalog_where(query, include_search=False, include_field=False)
            fields = [dict(r) for r in con.execute(
                'SELECT field value,COUNT(*) count FROM catalog_items WHERE ' + where +
                " AND field<>'' GROUP BY field ORDER BY count DESC,value LIMIT 60", args
            )]
            source = one(query, 'source')
            fwhere, fargs = ['i.active=1'], []
            if source in SOURCE_MAP:
                fwhere.append('i.source=?')
                fargs.append(SOURCE_MAP[source])
            formats = [dict(r) for r in con.execute(
                'SELECT f.format value,COUNT(*) count FROM item_formats f JOIN catalog_items i ON i.uid=f.uid '
                'WHERE ' + ' AND '.join(fwhere) + ' GROUP BY f.format ORDER BY count DESC,f.format LIMIT 40', fargs
            )]
            return {'fields': fields, 'formats': formats}
        finally:
            con.close()

    def field_names(self):
        try:
            con = connect(DB_PATH, readonly=True)
            try:
                return [r[0] for r in con.execute(
                    "SELECT field FROM catalog_items WHERE active=1 AND field<>'' GROUP BY field ORDER BY COUNT(*) DESC")]
            finally:
                con.close()
        except Exception:
            return []

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == '/api/my':
                return self.save_my()
            if path == '/api/settings':
                body = self._body()
                ai_service.save_settings(body if isinstance(body, dict) else {})
                return self._json(200, ai_service.public_settings())
            if path == '/api/ai/recommend':
                return self.ai_recommend()
            if path == '/api/recommendations/delete':
                body = self._body()
                return self._json(200, ai_service.delete_reco(str(body.get('id') or '')))
            return self._json(404, {'error': 'not found'})
        except ai_service.AiError as e:
            return self._json(400, {'error': str(e)})
        except Exception as e:  # noqa
            return self._json(500, {'error': repr(e)})

    def save_my(self):
        try:
            data = self._body()
            assert isinstance(data, dict) and isinstance(data.get('items'), dict)
        except Exception as e:
            return self._json(400, {'error': 'invalid json: %r' % e})
        data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        with LOCK:
            os.makedirs(os.path.dirname(MY_PATH), exist_ok=True)
            tmp = MY_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, MY_PATH)
        return self._json(200, {'ok': True, 'updated_at': data['updated_at'], 'count': len(data['items'])})

    def ai_recommend(self):
        body = self._body()
        query = str(body.get('query') or '').strip()
        model = str(body.get('model') or '').strip()
        result = ai_service.recommend(query, model=model, aihub_access=load_aihub_access(),
                                      fields_available=self.field_names(),
                                      refresh=bool(body.get('refresh')))
        return self._json(200, result)


def make_servers():
    """localhost 가 IPv6(::1)로 먼저 해석되면 IPv4 전용 바인딩에서는 요청마다
    수 초씩 지연된다. 루프백 IPv4·IPv6 양쪽에서 받도록 두 개를 띄운다."""
    servers = []
    for family, host in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
        try:
            klass = type('Srv', (ThreadingHTTPServer,), {'address_family': family})
            servers.append(klass((host, PORT), Handler))
        except OSError as e:
            print('  %s 바인딩 건너뜀: %s' % (host, e), flush=True)
    return servers


if __name__ == '__main__':
    prepare_catalog()
    servers = make_servers()
    if not servers:
        print('포트 %d 를 열지 못했습니다. 이미 실행 중인지 확인하세요.' % PORT)
        sys.exit(1)
    url = 'http://localhost:%d/' % PORT
    print('통합 데이터 카탈로그: %s   (종료: Ctrl+C 또는 창 닫기)' % url, flush=True)
    print('내 데이터셋 저장 위치: %s' % MY_PATH, flush=True)
    print('AI 설정 저장 위치: %s (git 제외)' % ai_service.SETTINGS_PATH, flush=True)
    if '--no-browser' not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for srv in servers:
            srv.shutdown()
