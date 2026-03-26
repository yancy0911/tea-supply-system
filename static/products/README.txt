Put SKU-matched product images here.

Naming convention:
- Use the Product SKU as filename, e.g. `T010103.jpg`
- Recommended: square or near-square images, 800px–1600px

Fallback behavior on /shop/:
- If `product.image_url` is present, it is used.
- Else we try `/static/products/{SKU}.jpg`.
- If that fails, we fall back to `/static/products/_fallback.jpg`,
  then finally to a MO·CHA CDN scene image (remote).

