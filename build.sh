#!/bin/sh
# Cloudflare Pages 빌드 스크립트
# data/snapshot/*.jsonl.gz -> site/ (정적 JSON 샤드)
set -e
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
echo "Python: $($PY --version)"
$PY build_static.py --out site
echo "빌드 산출물:"
du -sh site 2>/dev/null || true
