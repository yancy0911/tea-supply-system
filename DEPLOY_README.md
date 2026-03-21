# 部署说明（Django + Gunicorn + WhiteNoise）

本仓库已做**部署准备**（不新增业务功能）：环境变量、`STATIC_ROOT`、`WhiteNoise`、Gunicorn/Procfile。

## 环境变量（生产）

| 变量 | 必填 | 说明 |
|------|------|------|
| `DJANGO_SECRET_KEY` | **生产必填** | 随机长字符串；**禁止**使用仓库默认的 insecure key |
| `DJANGO_DEBUG` | 可选 | `False` / `0` / `no` 表示关闭调试；未设置时默认 `True`（本地开发） |
| `DJANGO_ALLOWED_HOSTS` | **生产必填** | 逗号分隔，如 `example.com,www.example.com` |

## 部署步骤（概要）

1. **安装依赖**

   ```bash
   pip install -r requirements.txt
   ```

2. **迁移数据库**

   ```bash
   python manage.py migrate
   ```

3. **收集静态文件到 `STATIC_ROOT`（生产需要）**

   - 使用 `WhiteNoise` 时，部署后应执行 `collectstatic`，将 `STATICFILES_DIRS` / 各 app 的 static 收集到 `staticfiles/`。
   - 本地开发若未执行 `collectstatic`，仍可通过 Django `runserver` 的静态查找与 `STATICFILES_DIRS` 访问；生产环境请执行：

   ```bash
   python manage.py collectstatic --noinput
   ```

4. **启动 Gunicorn**

   ```bash
   gunicorn tea_supply.wsgi:application --bind 0.0.0.0:8000
   ```

   或使用平台提供的 `Procfile`（见仓库根目录 `Procfile`）。

## 本地如何先验证 Gunicorn

在项目根目录、虚拟环境已激活的前提下：

```bash
export DJANGO_DEBUG=False
export DJANGO_SECRET_KEY="local-test-only-change-me"
export DJANGO_ALLOWED_HOSTS="127.0.0.1,localhost"

python manage.py migrate
python manage.py collectstatic --noinput

gunicorn tea_supply.wsgi:application --bind 127.0.0.1:8000
```

浏览器访问 `http://127.0.0.1:8000/` 验证页面与静态资源。

## 常见问题

- **`collectstatic` 是否必须？**  
  - **生产**：建议必须执行（`STATIC_ROOT` 指向 `staticfiles/`，由 WhiteNoise 提供）。  
  - **仅本地 `runserver` + DEBUG**：可不执行，但生产/ Gunicorn 下应执行。

- **`migrate` 如何执行？**  
  - 每次部署新版本代码后，在对应环境执行一次 `python manage.py migrate`（与数据库文件或远程 DB 一致）。

## Python 版本

见 `runtime.txt`（与当前开发环境兼容的 3.9.x）。
