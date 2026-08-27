# -*- coding: utf-8 -*-
"""
로컬 대시보드 서버
  python serve.py            # http://localhost:8765 에서 index.html 제공 + 내 데이터셋(data/my_datasets.json) 읽기/저장 API
  GET  /api/my   -> data/my_datasets.json 내용
  POST /api/my   -> 본문(JSON)을 data/my_datasets.json 에 저장
"""
import os, json, sys, time, webbrowser, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
MY_PATH = os.path.join(ROOT, 'data', 'my_datasets.json')
PORT = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8765)
LOCK = threading.Lock()


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

    def end_headers(self):
        if self.path.endswith('.json') or self.path.endswith('.html') or self.path == '/':
            self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def do_GET(self):
        if self.path.split('?')[0] == '/api/my':
            with LOCK:
                if os.path.exists(MY_PATH):
                    try:
                        data = json.load(open(MY_PATH, encoding='utf-8'))
                    except Exception as e:
                        return self._json(500, {'error': repr(e)})
                else:
                    data = {'updated_at': None, 'items': {}}
            return self._json(200, data)
        if self.path == '/':
            self.path = '/index.html'
        return super().do_GET()

    def do_POST(self):
        if self.path.split('?')[0] != '/api/my':
            return self._json(404, {'error': 'not found'})
        n = int(self.headers.get('Content-Length') or 0)
        try:
            data = json.loads(self.rfile.read(n).decode('utf-8'))
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


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    url = 'http://localhost:%d/' % PORT
    print('AI-Hub 대시보드: %s   (종료: Ctrl+C 또는 창 닫기)' % url, flush=True)
    print('내 데이터셋 저장 위치: %s' % MY_PATH, flush=True)
    if '--no-browser' not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
