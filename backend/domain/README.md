# Your domain files

This folder holds everything the assistant knows about **your** database beyond its structure.
The agents read it at startup; no code changes are needed.

| File | What goes in it | Required? |
|---|---|---|
| `business_rules.md` | Business terms, table grain, tricky joins, status codes, metrics that must not be confused | Recommended |
| `few_shots.md` | Real questions with their correct T-SQL | Recommended |
| `golden.jsonl` | Test questions for `python -m eval.run_eval` | Optional |

All three files can be empty. The assistant still works from the live schema alone; the files
make it more accurate on your specific data.

**Start here:** read [docs/writing-domain-files.md](../../docs/writing-domain-files.md), and look
at the complete working example in [examples/adventureworks/](../../examples/adventureworks/).

To keep your files somewhere else (for example a private folder outside this repository), set
`DOMAIN_DIR` in `backend/.env`.

## golden.jsonl format

One JSON object per line:

```json
{"question": "How many orders were placed in 2024?", "expect_tables": ["sales.orders"], "notes": "Count orders, not order lines."}
```

`expect_tables` lists the tables the generated SQL must read for the case to pass. `notes` is for
people only.
