## 部署前检查清单

在推到生产/上云之前，请逐项确认，避免“上线即翻车”。

---

### 1. 数据库迁移（migrate）

1. 确认当前分支的 migration 已全部提交
2. 执行：
   - `python manage.py makemigrations`（通常不需要，但可确认没有漏）
   - `python manage.py migrate`
3. 验证：
   - admin 能正常打开 `Customer/Order/StockLog` 等关键页面

---

### 2. 静态资源（static）

1. 静态目录是否存在并可访问：
   - `static/`（至少包含：页面展示用的二维码图片等）
2. 验证二维码图片等静态资源：
   - `http://<host>/static/wechat_qr.png`（或你的实际文件名）
3. 若使用 `collectstatic`，确保 CI/CD 阶段无报错

---

### 3. 数据库备份（强烈建议）

上线前在服务器上先备份 sqlite：
1. 备份 `db.sqlite3`（见《数据备份与恢复说明》）
2. 如有 `media/` 资源，也请一起备份（见下文）

---

### 4. admin 账号检查

1. 确认至少有 1 个管理员能登录 `admin/`
2. 在 admin 中检查：
   - `Customer.account_status` 能否编辑并保存
   - `Order` 列表与 `workflow_status` 流转表单可用

---

### 5. 测试账号检查

1. 准备（或保留）测试手机号/测试客户
2. 确认“待审核 / 已通过 / 已禁用”三种状态的测试账号都存在
3. 确认商城下单成功流程可在“已通过”客户上运行

