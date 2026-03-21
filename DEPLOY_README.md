# 部署说明（Django + Gunicorn + WhiteNoise + Railway）

本仓库已做**部署准备**（不新增业务功能）：环境变量、`STATIC_ROOT`、`WhiteNoise`、Gunicorn/Procfile、Railway 兼容（缺省不崩溃、不 DisallowedHost）。

## Railway 推荐环境变量

| 变量 | 是否必须 | 说明 |
|------|----------|------|
| `DJANGO_SECRET_KEY` | **强烈建议** | 随机长字符串；未设置时项目仍可启动，但**不安全**，上线后务必在 Railway 面板配置 |
| `DJANGO_DEBUG` | 可选 | `False` / `0` / `no` 关闭调试；**未设置时**：检测到 Railway 环境则默认 `False`，本地默认 `True` |
| `DJANGO_ALLOWED_HOSTS` | 可选 | 逗号分隔域名；**未设置或为空**时默认为 `["*"]`，避免临时域名/首部署出现 `DisallowedHost`；稳定后建议改为你的正式域名 |
| `PORT` | 由平台注入 | Railway 自动设置；`Procfile` 已使用 `$PORT` |

## Railway 最简重新部署步骤

1. 将代码推送到 Railway 关联的分支（触发构建/部署）。
2. 在 Railway **Variables** 中至少设置 `DJANGO_SECRET_KEY`（推荐同时设置 `DJANGO_DEBUG=False`）。
3. **Build**（或在 Start Command 前执行一次）：
   ```bash
   python manage.py collectstatic --noinput
   ```
4. **Release / 启动前迁移**（二选一，按你平台习惯）：
   ```bash
   python manage.py migrate --noinput
   ```
5. 启动命令已由根目录 `Procfile` 提供：
   ```text
   web: gunicorn tea_supply.wsgi:application --bind 0.0.0.0:$PORT
   ```

> SQLite：默认使用仓库内 `db.sqlite3`。Railway 文件系统若为**临时盘**，重启可能丢库；长期生产建议改为托管数据库（属架构升级，不在本次「仅部署修复」范围）。

## 通用：安装与迁移

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
```

## 本地验证 Gunicorn（模拟生产）

```bash
export DJANGO_DEBUG=False
export DJANGO_SECRET_KEY="local-test-only-change-me"
export DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost"
export PORT=8000

python manage.py migrate
python manage.py collectstatic --noinput

gunicorn tea_supply.wsgi:application --bind 127.0.0.1:${PORT}
```

## Python 版本

见 `runtime.txt`。
