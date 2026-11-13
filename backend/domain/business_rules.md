<!--
  BUSINESS RULES — your database's business knowledge, in plain words.

  This file is read by the SQL Author, the Analyst, the Verifier and the Synthesizer. Write the
  things a new analyst would need to be told before they could answer questions correctly: what
  the business words mean, which table answers what, and where the data is tricky.

  Everything inside these comment markers is removed before the agents see the file, so you can
  leave or delete these notes. A file that holds only headings and comments counts as empty, and
  the agents then work from the live schema alone.

  Tips (see docs/writing-domain-files.md for the full guide):
    * Name real tables and columns as schema.table / table.column, exactly as in the database.
    * Prefer one precise sentence over a paragraph. Tables are easy for the model to follow.
    * Write a rule only when you are sure of it. Put open questions under "Not yet defined".
    * For a working example, see examples/adventureworks/business_rules.md.
-->

## 1. What this data is about

<!-- One short paragraph: the business, who uses the data, the main entities.
     e.g. "Orders placed by customers for products, shipped from our warehouses." -->

## 2. Glossary — business word → where it lives

<!-- | Business term | Meaning | Table / column |
     |---|---|---|
     | Revenue | Order value before tax and freight | sales.orders.subtotal |
     | Active customer | Ordered in the last 12 months | sales.orders.order_date | -->

## 3. Table grain and duplicates

<!-- Which tables hold MORE than one row per entity, and how to count them correctly.
     e.g. "sales.order_lines has one row per product per order: count orders with
     COUNT(DISTINCT order_id), never COUNT(*)." -->

## 4. Joins that are not obvious

<!-- Joins the foreign keys do not show, or that go through a bridge table.
     e.g. "A customer's name is in crm.person (individuals) OR crm.company (businesses)." -->

## 5. Status codes and flags

<!-- What coded values mean.
     e.g. "orders.status: 1 = open, 2 = shipped, 3 = cancelled." -->

## 6. Never confuse these

<!-- Pairs of similar columns or metrics that must not be swapped.
     e.g. "order_date (placed) vs ship_date (sent) vs due_date (promised)." -->

## 7. Not yet defined

<!-- Business questions that have no agreed rule yet. The agents will say so instead of guessing.
     e.g. "What counts as a 'late' delivery has not been agreed." -->
