# Writing your domain files

The assistant reads your database's **structure** automatically: tables, columns, types, primary
and foreign keys, tables that hold several rows per key, and real values from small lookup tables.

What it cannot read is **meaning**. That is what the domain files are for. They live in
`backend/domain/` (or wherever `DOMAIN_DIR` in `backend/.env` points):

| File | Read by | Purpose |
|---|---|---|
| `business_rules.md` | SQL Author, Analyst, Verifier, Synthesizer | What your data means and where it is tricky |
| `few_shots.md` | SQL Author | Proven question → SQL patterns to copy |
| `golden.jsonl` | `python -m eval.run_eval` only | Test questions to measure accuracy |

A full working set for the AdventureWorks sample database is in
[examples/adventureworks/](../examples/adventureworks/). Reading it is the fastest way to learn
the style.

## How the files are used

* They are loaded **once at startup**. Restart the API (or CLI) after editing them.
* Anything inside `<!-- ... -->` is removed first, so notes for people cost no tokens.
* A file containing only headings and comments counts as **empty**. Empty is fine: the agents
  still answer from the schema alone. A **wrong** rule is worse than no rule.
* Everything in these files is sent to your LLM provider on every question, so keep them
  focused. A few hundred lines is fine; whole manuals are not.

## business_rules.md

Write what a new analyst would have to be told before answering questions correctly. The
template has seven sections; use the ones that apply.

### 1. What this data is about
Two or three sentences. Also state anything that changes how dates work — for example "the data is
historical; measure 'last month' from the latest order date, not today".

### 2. Glossary
Map every business word your users say to the table and column that holds it. This is the most
valuable section.

```markdown
| Business term | Meaning | Table / column |
|---|---|---|
| Revenue | Order value before tax and freight | `sales.orders.subtotal` |
| Active customer | Ordered in the last 12 months | `sales.orders.order_date` |
```

### 3. Table grain and duplicates
The most common source of wrong numbers. Say which tables hold several rows per entity, and how
to count them.

* "`order_lines` has one row per product per order. Count orders with
  `COUNT(DISTINCT order_id)`."
* "Do not add up the order total after joining to order lines — it repeats on every line."
* "`stock_snapshot` holds one row per product per day. For current stock use the latest date."

The schema reader already flags tables with repeated keys (`⚠ MANY ROWS PER ...`), but it cannot
tell *why*, or which row is the right one.

### 4. Joins that are not obvious
Joins that the foreign keys do not show, go through a bridge table, or have two possible paths.

* "A customer's name is in `crm.person` (individuals) or `crm.company` (businesses)."
* "`orders.region_code` joins `ref.region.code`, not `ref.region.id`."

### 5. Status codes and flags
Spell out every coded value users ask about: `1 = open, 2 = shipped, 3 = cancelled`. Small lookup
tables are read automatically, but codes stored only as numbers in a big table are not.

### 6. Never confuse these
Pairs of similar columns or metrics: placed vs shipped vs due date, gross vs net, list price vs
price charged, current value vs history.

### 7. Not yet defined
Questions with no agreed answer yet. The agents will say a rule is not defined instead of
inventing one — and the Verifier will not reject a query just because of it.

### Style tips
* Name real objects exactly as in the database: `schema.table`, `table.column`.
* One precise sentence beats a paragraph. Tables are easy for the model to follow.
* Write rules as facts, not hopes. If you are unsure, put it under "Not yet defined".
* Never put passwords, personal data or secrets in these files.

## few_shots.md

Each example is a `Q:` line followed by one ` ```sql ` block:

````markdown
Q: How many orders were shipped late?
```sql
SELECT COUNT(*) AS late_orders
FROM sales.orders
WHERE ship_date > due_date;
```
````

* **Pick your hardest patterns**, not your easiest: de-duplication, the right join path, latest
  row per entity, relative dates, a business definition from your rules.
* **Run every query** against the database before adding it. A broken example teaches the model
  to be broken.
* 8–15 examples is usually right. More costs tokens on every question for little gain.
* Write T-SQL (SQL Server): `TOP` not `LIMIT`, `DATEADD` / `DATEDIFF`, `[brackets]` for names
  with spaces or reserved words.
* Comments inside the SQL (`-- ...`) are a good place for a one-line reason.

## golden.jsonl

One JSON object per line. Used only by `python -m eval.run_eval`:

```json
{"question": "How many orders were shipped late?", "expect_tables": ["sales.orders"], "notes": "ship_date > due_date"}
```

* `expect_tables` — lower-case `schema.table` names the generated SQL must read for a pass.
* `notes` — for people; what a correct answer needs.

Run the eval after every change to the rules, the examples or the model, and compare the results.

## .env settings that belong with your domain

```ini
APP_NAME=Sales Insights                 # shown in the UI, API docs and export file names
APP_DESCRIPTION=Orders, customers and stock of an online shop
ALLOWED_SCHEMAS=sales,inventory         # blank = every user schema
EXCLUDED_TABLES=sales.orders_backup     # copies, staging, anything irrelevant
EXCLUDED_COLUMNS=customer.tax_id        # personal or sensitive columns
```

After changing schemas or exclusions run `python -m app.db.introspect`.
