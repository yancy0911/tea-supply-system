"""django-import-export：后台 Product / ProductCategory CSV 导入导出。"""
from import_export import fields, resources
from import_export.widgets import ForeignKeyWidget

from tea_supply.models import Product, ProductCategory


class ProductCategoryResource(resources.ModelResource):
    """分类：按 name 匹配更新或新建。"""

    class Meta:
        model = ProductCategory
        import_id_fields = ("name",)
        fields = ("name", "sort_order", "is_active")


class ProductResource(resources.ModelResource):
    """商品：CSV 列 category 为分类名称；按 sku 更新或新建。"""

    category = fields.Field(
        column_name="category",
        attribute="category",
        widget=ForeignKeyWidget(ProductCategory, "name"),
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
