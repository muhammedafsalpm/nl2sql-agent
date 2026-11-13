# Security policy

NL2SQL Agent connects a language model to a real database, so security reports are taken
seriously.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Instead, use GitHub's private
[**Report a vulnerability**](https://github.com/muhammedafsalpm/nl2sql-agent/security/advisories/new)
form on this repository.

Include what you found, how to reproduce it, and the impact you expect. You will get a response
as soon as possible, and credit in the fix notes if you would like it.

Especially relevant:

- a way to make the agent run anything other than a single read-only `SELECT`
  (bypassing `backend/app/graph/nodes/validator.py`);
- a way to read tables or columns hidden by `ALLOWED_SCHEMAS`, `EXCLUDED_TABLES` or
  `EXCLUDED_COLUMNS`;
- forging or replaying export tokens (`/api/export`);
- leaking secrets from `.env` through the API or the UI.

## Deploying safely

- Connect with a **read-only** SQL login (`db_datareader` only), never `sa` or a database owner.
  `python backend/scripts/check_setup.py` warns if the login can write.
- Set `API_KEY`, restrict `CORS_ORIGINS`, set a stable `EXPORT_SECRET`, and put the API behind
  TLS before exposing it beyond your own machine.
- Everything the agents read (schema, domain files, retrieved rows) is sent to the configured LLM
  provider. Use a local model (`LLM_PROVIDER=local` or a local `OPENAI_BASE_URL`) if your data must
  not leave your network.
- Never commit `backend/.env`; it is git-ignored by default.
