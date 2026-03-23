#!/usr/bin/env bash
# 在 Render Shell 于「项目根目录」（与 manage.py 同级）执行，从仓库内 data/products_import_ready.csv
# 生成 /tmp/products.csv 与 /tmp/product_categories.csv（无需从本机上传文件）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT/data/write_products_csv_tmp.py"
