# 奶茶供应系统 — 交付说明

本文档面向交付对象（客户/合作方/内部试用），说明网址、账号、角色分工与基本操作。**业务规则以线上代码为准**；若与本文不一致，以系统实际行为为准。

---

## 1. 系统网址

- **部署后请替换为实际域名**，例如：`https://your-app.onrender.com` 或自有域名。
- 本地开发默认：`http://127.0.0.1:8000/`（以运行 `python manage.py runserver` 的地址为准）。

---

## 2. 登录方式

- 打开 **`/login/`** 使用用户名与密码登录。
- 商城入口 **`/shop/`** 可引导至登录；登录成功且未带安全 `next` 参数时，**按角色进入默认首页**（见下表）。
- 若登录页 URL 带有合法的 `next`（同站相对路径或同源绝对地址），登录成功后优先跳转到 `next`。
- 客户也可通过 **`/register/`** 自助注册（注册账号默认为**客户**角色）。

---

## 3. 建议测试账号（需在环境中创建）

以下为用户名 **建议命名**；若尚未创建，请在 Django Admin（`/admin/`）或 shell 中创建 `User`，并在 **用户角色（UserRole）** 中绑定对应 `role`。密码由部署方设置，**勿在公开仓库中写明文密码**。

| 用户名（建议） | 角色 | 登录后默认页面 | 权限说明（摘要） |
|----------------|------|----------------|------------------|
| `demo_owner` | owner（老板） | `/dashboard/` | 全站业务最高权限；可看报表、仪表盘、订单、库存；可任意推进订单状态（与订单状态机一致）；可进 Admin（若设为 superuser/staff）。 |
| `demo_manager` | manager（经理） | `/orders/` | 运营侧：批发录入、订单列表、老板仪表盘、订单确认/付款/取消等；**不能**看经营报表 `/reports/`、客户洞察 `/reports/customers/`。 |
| `demo_warehouse` | warehouse（仓库） | `/inventory/` | 库存、补货相关；订单详情（备货）；**不能**进订单列表 `/orders/`、报表、客户洞察。 |
| `demo_driver` | driver（司机） | `/driver/orders/` | 仅配送任务与完成配送；**不能**进库存、订单列表等。 |
| `demo_customer` | customer（客户） | `/shop/` | 商城浏览、下单、`/my-orders/`；**不能**进内部 `/dashboard/`、`/orders/`、`/inventory/` 等（会被重定向或 403）。 |

---

## 4. 角色权限说明

### 4.1 能看哪些页面（概要）

| 页面 / 功能 | owner | manager | warehouse | driver | customer |
|-------------|:-----:|:-------:|:---------:|:------:|:--------:|
| `/shop/` 商城 | ✓ | ✓ | ✓ | ✓ | ✓ |
| `/my-orders/`、`/credit/`（需客户档案） | ✓ | ✓ | ✓ | ✓ | ✓（有档案时） |
| `/dashboard/` 运营仪表盘 | ✓ | ✓ | ✗ | ✗ | ✗（重定向商城） |
| `/` 批发录单 | ✓ | ✓ | ✗ | ✗ | ✗ |
| `/orders/` 订单列表 | ✓ | ✓ | ✗ | ✗ | ✗ |
| `/orders/<id>/status/` 订单详情/状态 | ✓ | ✓ | ✓ | ✗ | ✗ |
| `/inventory/` 库存 | ✓ | ✗ | ✓ | ✗ | ✗ |
| `/replenishment/` 补货 | ✓ | ✗ | ✓ | ✗ | ✗ |
| `/driver/orders/` 司机配送 | ✗ | ✗ | ✗ | ✓ | ✗ |
| `/reports/` 经营报表 | ✓ | ✗ | ✗ | ✗ | ✗ |
| `/reports/customers/`、`/customers/` 客户洞察 | ✓ | ✗ | ✗ | ✗ | ✗ |

说明：`/driver/orders/` 仅 **driver** 可访问（`role_required`）；老板在仪表盘处理「发往配送」等，不进入司机端页面。

### 4.2 不能看哪些页面（概要）

- **customer**：内部仪表盘、批发录单、订单列表、库存、报表、客户洞察等（非商城路径多会被重定向到 `/shop/` 或 403）。
- **manager**：`/reports/`、`/reports/customers/`、`/customers/`、`/inventory/`、`/replenishment/` 等（非其角色）。
- **warehouse**：`/orders/`、`/dashboard/`（及部分运营页）等。
- **driver**：库存、订单列表、报表等。

### 4.3 能操作什么按钮（订单相关，摘要）

- **owner**：订单详情中可保存结算方式、付款信息等；可按 `order_status_flow` 允许的规则推进状态（含任意合法跳转，以代码为准）。
- **manager**：确认订单、标记已付款、取消、发往配送（在允许的状态下）等。
- **warehouse**：例如「开始备货」类操作（`paid` → `picking`，以系统按钮为准）。
- **driver**：仅 **`assigned_driver` 为本人** 的订单可「完成配送」（`shipping` → `completed`）。
- **customer**：不修改订单内部状态（仅商城下单、查看自己的订单等）。

### 4.4 订单状态可推进到哪一步（业务摘要）

标准生命周期大致为：`pending` → `confirmed` → `paid` → `picking` → `shipping` → `completed`（另有 `cancelled`）。  
**谁可在哪一步操作** 由后端 `can_transition` / `apply_transition` 与角色共同约束，不允许非法跳步；详见实现模块 `tea_supply/order_status_flow.py`。

---

## 5. 基本操作流程

1. **客户下单**：登录商城 → 选品 → 结账提交 → 在「我的订单」或成功页查看订单号。
2. **运营确认订单**：经理/老板在订单列表或详情中 **确认订单**（`pending` → `confirmed`），并视情况 **标记已付款**（→ `paid`）。
3. **仓库处理库存**：仓库在订单详情执行备货类操作（`paid` → `picking`）；备货扣减逻辑以系统信号与订单状态为准。
4. **司机配送**：运营将订单发往配送（→ `shipping`）并指派司机；司机在 **`/driver/orders/`** 完成配送（→ `completed`）。

---

## 6. 初始库存说明

- 商品与库存可来自 CSV 导入、`bootstrap_full_shop` 等管理命令，或 Admin 维护。
- **交付环境若为演示数据**，请在文档或口头说明「数据仅供演示，上线前请替换为真实商品与库存」。
- 是否「演示库存」取决于当前数据库内容，部署时请自行核对 `Product` / 库存字段。

---

## 7. 后续如何修改商品 / 库存 / 账号

- **商品与价格**：Django Admin 中维护 `Product`、`ProductCategory` 等；或使用项目内既有导入命令（见 `tea_supply/management/commands/`）。
- **库存**：Admin 或业务侧库存页（视权限），并注意与订单备货逻辑一致。
- **账号与角色**：在 Admin 中管理 `User`；每个用户绑定一条 **`UserRole`**，角色字段决定内部权限。
- **超级用户**：`createsuperuser` 创建；`get_effective_role` 将 superuser 视为 **owner** 级能力（与报表等一致）。

---

## 8. 登录后默认去向（实现说明）

实现位于 `tea_supply/rbac.py` 的 `get_post_login_redirect`：在 **`next` 非空且安全、且路径符合该角色允许的前缀** 时优先使用；否则按上表跳转至各角色默认首页。

---

## 9. 在线帮助页

登录后可访问 **`/help/`** 查看简要入口与流程（模板 `templates/delivery_help.html`）。

---

## 10. 仓库内其他文档

- 根目录 **`README.md`**：开发与运行入口说明。
