# Makro fixtures

`anonymous.html` is a sanitized rendered capture of the current public food category at
`https://sortiment.makro.cz/shop/category/potraviny`, taken 2026-10-01 with normal Chromium.
It shows 24 of 17,601 products at makro Praha - Stodůlky, and login prompts instead of prices.
`robots.txt` is the live site's robots document captured the same day.

`tests/makro_support.py` replaces four cards' names, packaging and missing prices with
**synthetic financial examples**. These are not captured customer prices. The price layout
and Czech VAT labels follow the site's publicly served UI definitions. They test the
production parser, acquisition completeness, package mathematics and persistence without login.
