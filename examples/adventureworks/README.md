# Example: AdventureWorks

A complete, working domain pack for **AdventureWorks**, Microsoft's free sample database for SQL
Server. AdventureWorks is a fictional bicycle company with sales, products, inventory,
manufacturing, purchasing and HR data, so it exercises every agent in the pipeline.

Use it to try the assistant in a few minutes, and as a model for writing your own domain files.

| File | Contents |
|---|---|
| `business_rules.md` | Glossary, table grain, non-obvious joins, status codes, metrics not to confuse |
| `few_shots.md` | 14 questions with correct T-SQL |
| `golden.jsonl` | 16 test questions for `python -m eval.run_eval` |

## 1. Get the database

1. Install **SQL Server** (the free Developer or Express edition) and **SQL Server Management
   Studio** or Azure Data Studio.
2. Download the OLTP backup `AdventureWorks2022.bak` (or 2019/2017) from Microsoft's
   [AdventureWorks sample databases page](https://learn.microsoft.com/sql/samples/adventureworks-install-configure).
3. Restore it (in SSMS: right-click **Databases → Restore Database → Device**, pick the `.bak`).

## 2. Create a read-only login

Run this in the restored database, with your own password:

```sql
CREATE LOGIN nl2sql_ro WITH PASSWORD = '<choose-a-strong-password>';
USE AdventureWorks2022;
CREATE USER nl2sql_ro FOR LOGIN nl2sql_ro;
ALTER ROLE db_datareader ADD MEMBER nl2sql_ro;
-- Optional: lets the assistant notice new data quickly (row-count statistics).
GRANT VIEW DATABASE STATE TO nl2sql_ro;
```

## 3. Point the assistant at it

In `backend/.env`:

```ini
APP_NAME=AdventureWorks Assistant
APP_DESCRIPTION=Sales, products, inventory, manufacturing, purchasing and employees of a bicycle manufacturer
DOMAIN_DIR=../examples/adventureworks

DB_SERVER=localhost
DB_PORT=1433
DB_NAME=AdventureWorks2022
DB_USER=nl2sql_ro
DB_PASSWORD=<the password you chose>

# The business schemas. dbo only holds build/log tables in this sample.
ALLOWED_SCHEMAS=sales,production,purchasing,humanresources,person
# Personal identifiers, and column types the ODBC driver cannot read (hierarchyid, geography, xml).
EXCLUDED_COLUMNS=creditcard.cardnumber,employee.nationalidnumber,organizationnode,spatiallocation,documentnode,demographics,additionalcontactinfo,catalogdescription,instructions,resume,rowguid
```

Then, from `backend/`:

```bash
python -m app.db.introspect     # read the schema
python -m eval.run_eval         # run the 16 test questions
python serve.py                 # start the API, then `npm run dev` in frontend/
```

## Questions to try

* How many orders are there?
* Show revenue by product category as a donut chart
* Who are our top 10 customers by revenue?
* Which territory has the highest sales? Compare online and reseller revenue.
* Which products have fewer than 100 units in stock?
* How many employees work in each department?
* Which work orders were completed late, and by how many days?
* Predict which product categories will grow next year, based on the yearly trend
