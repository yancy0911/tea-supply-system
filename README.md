# 奶茶供应系统（Tea Supply）

Django 项目：B2B 批发 / 商城下单、库存、配送与订单状态流。

## 快速开始

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

浏览器访问 `http://127.0.0.1:8000/`。登录页：`/login/`；商城：`/shop/`。

## 交付与角色说明

面向交付对象的使用说明、测试账号建议、角色权限与业务流程见：

**[docs/DELIVERY.md](docs/DELIVERY.md)**

登录后默认首页按角色跳转（老板 → `/dashboard/`，经理 → `/orders/`，仓库 → `/inventory/`，司机 → `/driver/orders/`，客户 → `/shop/`），实现见 `tea_supply/rbac.py` 中 `resolve_login_redirect_url`。

简要在线帮助（需登录）：`/help/`。

## 技术栈

- Python 3.12.x、Django 4.2（见 `requirements.txt`）

## 许可与维护

内部/客户交付项目；部署与密钥请勿提交到公开仓库。
