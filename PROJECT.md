# 供应链与库存（摘要）

## 库存与订单

- 商品 **`stock_quantity`**（无原材料时）；有关联原材料则扣 **`Ingredient.stock`**。
- **待确认**下单不扣库存，仅校验；**备货中**一次性扣减并记 **`StockLog`**；取消/退回按原逻辑恢复。
- **库存流水**：`/admin/tea_supply/stocklog/`（只读列表，系统写入）。

## 补货页 `/replenishment/`（老板决策版）

- 近 **7 / 30 天销量**（订单明细，排除待确认、已取消）。
- **日均** = 近30天 ÷ 30；**可卖天数** = 库存 ÷ 日均（无销量时不算天数）。
- **建议采购量** = max(0, **60×日均 − 当前库存**)。
- **风险分级**（规则见 `tea_supply/views.py` 中 `_replenishment_risk_and_action`）：
  - 近30天销量为 **0**：**观察中**，不判红/黄/绿；销量列显示「暂无销量参考」。
  - **红**：可卖天数 ≤7 **或** 库存 &lt; 近30天销量的 25%。
  - **黄**（非红）：可卖天数 ≤15 **或** 库存 &lt; 近30天销量的 50%。
  - **绿**：其余。
- 排序：红 → 黄 → 绿 → 观察；同级按可卖天数从少到多。

## 命令

```bash
./.venv/bin/python manage.py migrate
# 端到端：规范化 CSV → 导入分类/商品 → 清洗字段 → 补图（对当前 DATABASE_URL / 本地 sqlite 生效）
python manage.py bootstrap_full_shop
# 仅重复清洗（已导过 CSV）
python manage.py bootstrap_full_shop --skip-csv
```

- 后台商品 **Import**：`/admin/tea_supply/product/`（django-import-export）。
- **Render 生产库**：在 **Shell** 中执行 `python manage.py bootstrap_full_shop`（依赖仓库内 `data/products_import_ready.csv` 与环境变量 `DATABASE_URL`）。
- `releaseCommand` 仍为 `migrate`；全量导入请在部署后 Shell 跑 `bootstrap_full_shop`。
