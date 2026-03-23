"""django-import-export：后台 Product / ProductCategory CSV 导入导出。

与 data/extract_mocha_pdf_cards.py 生成的 products_import_ready.csv 表头一致（含 cost/stock/units_per_case）。
"""
from import_export import fields, resources
from import_export.widgets import BooleanWidget, ForeignKeyWidget

from tea_supply.models import Product, ProductCategory


class ProductCategoryResource(resources.ModelResource):
    """分类：按 name 匹配更新或新建。"""

    class Meta:
        model = ProductCategory
        import_id_fields = ("name",)
        fields = ("name", "sort_order", "is_active")


class _Bool01Widget(BooleanWidget):
    """接受 CSV 里 1/0、true/false。"""

    def clean(self, value, row=None, **kwargs):
        if value is None or str(value).strip() == "":
            return True
        s = str(value).strip().lower()
        if s in ("0", "false", "no", "n"):
            return False
        if s in ("1", "true", "yes", "y"):
            return True
        return super().clean(value, row, **kwargs)


class ProductResource(resources.ModelResource):
    """商品：CSV 列 category 为分类名称；按 sku 更新或新建。"""

    category = fields.Field(
        column_name="category",
        attribute="category",
        widget=ForeignKeyWidget(ProductCategory, "name"),
    )
    can_split_sale = fields.Field(
        column_name="can_split_sale",
        attribute="can_split_sale",
        widget=_Bool01Widget(),
    )
    is_active = fields.Field(
        column_name="is_active",
        attribute="is_active",
        widget=_Bool01Widget(),
    )

    class Meta:
        model = Product
        import_id_fields = ("sku",)
        fields = (
            "category",
            "name",
            "sku",
            "unit_label",
            "case_label",
            "price_single",
            "price_case",
            "cost_price_single",
            "cost_price_case",
            "shelf_life_months",
            "can_split_sale",
            "minimum_order_qty",
            "is_active",
            "stock_quantity",
            "units_per_case",
            "image",
        )
        export_order = fields
        skip_unchanged = True
