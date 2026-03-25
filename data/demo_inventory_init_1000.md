## Demo Inventory Initialization (1000)

Date: 2026-03-25

### Inventory init actions
- Set all `Product.stock` to `1000.0`
- Also mirrored: `Product.current_stock` = `1000.0`, `Product.stock_quantity` = `1000.0`
- Ensured: `Product.stock_enabled` = `true`, `Product.is_active` = `true`
- Ensured: `ProductCategory.is_active` = `true` (so `/shop/` can display products)

### Verification (Django checkout submit -> stock deducted)
- Total products updated: `170`
- Total categories enabled: `44`
- Tested order:
  - Product ID: `1`
  - SKU: `ING-1`
  - Sale type: `single`
  - Qty ordered: `0.01`
  - Expected stock deduction: `0.01`
  - Stock: `1000.0` -> `999.99`

