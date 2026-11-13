<!--
  FEW-SHOT EXAMPLES — questions your users ask, each with the correct T-SQL.

  The SQL Author copies the PATTERNS in these examples (joins, de-duplication, date logic), so a
  handful of good ones improves accuracy more than any other change. Aim for 8-15 examples that
  cover your trickiest cases, not your easiest ones.

  Format: a line starting with "Q:", then ONE ```sql block. Use real table and column names from
  your database, and run every query once to confirm it works before adding it.

  Everything inside these comment markers is removed before the agents see the file. A file that
  holds only comments counts as empty.

  Example (uncomment and adapt, or see examples/adventureworks/few_shots.md):

  Q: How many orders were placed in each year?
  ```sql
  SELECT YEAR(o.order_date) AS order_year, COUNT(*) AS order_count
  FROM sales.orders o
  GROUP BY YEAR(o.order_date)
  ORDER BY order_year;
  ```
-->
