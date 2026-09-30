# Kupi parser fixtures

Captured from public HTML on 2026-09-30:

- https://www.kupi.cz/slevy/ovoce-a-zelenina
- https://www.kupi.cz/slevy/ovoce-a-zelenina?page=2
- https://www.kupi.cz/robots.txt

These are reduced extracts of the actual listing markup. Product headings, individual retailer
rows, prices, quantities, notes, loyalty labels, validity text, locality, and source IDs are retained.
Navigation, advertisements, scripts, images, tracking attributes, and unrelated content are removed;
whitespace is normalized. Page two's pagination links are deliberately removed to provide a finite
two-page fixture scenario. Page one still exercises the actual next-page link. Robots text is unedited.

There are 54 rows on page one, 46 on page two, and 90 distinct campaign IDs overall. Duplicate
sponsored/recommended rows deliberately remain to exercise deduplication. Dates are interpreted
against an injected capture timestamp. Runtime snapshots retain downloaded bytes, not these reduced
fixtures. Do not replace fixture values with synthetic values without distinguishing that scenario.
