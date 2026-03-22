#!/usr/bin/env bash
# 使用项目内默认 PDF：tea_supply/data/mocha.pdf（可传参覆盖：--pdf /path/to.pdf）
set -e
cd "$(dirname "$0")/.."
exec python manage.py import_mocha_pdf "$@"
