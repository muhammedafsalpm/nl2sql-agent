# Contributing to NL2SQL Agent

Thanks for your interest! Contributions of every size are welcome: bug reports, documentation,
example domain packs, and code.

## Ways to help

- **Report a bug** — open an issue with the steps to reproduce, what you expected, and the
  relevant lines from `backend/logs/chatbot.log` (remove any data you cannot share).
- **Suggest a feature** — open an issue first so we can agree on the approach before you code.
- **Share a domain pack** — a `business_rules.md` + `few_shots.md` + `golden.jsonl` for a
  public sample database (see [examples/adventureworks/](examples/adventureworks/)) helps others
  learn how to describe their own data.
- **Improve the docs** — clearer setup steps and troubleshooting notes are always valuable.

## Development setup

```bash
# backend
cd backend
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
python scripts/check_setup.py

# frontend
cd ../frontend
npm install
npm run dev
```

The [README](README.md) explains the architecture and every setting.

## Pull requests

1. Fork the repo and create a branch from `main` (e.g. `fix/validator-comment-scan`).
2. Keep each pull request focused on one change.
3. Make sure it works:
   - `python -m compileall -q backend/app` passes;
   - `npm run build` passes in `frontend/`;
   - if you changed prompts, rules or SQL generation, run `python -m eval.run_eval` against a
     test database and mention the before/after result in the PR.
4. Update the README or `.env.example` when you add or change a setting.
5. Describe **what** changed and **why** in the PR description.

## Guidelines

- **Never commit secrets or real data** — no `.env`, API keys, passwords, connection strings,
  customer data or internal business rules. Use the AdventureWorks sample or made-up data.
- **Keep the core database-agnostic in content** — anything specific to one business belongs in
  a domain pack, not in Python code or prompts.
- **Safety first** — the SELECT-only validator and read-only execution are core guarantees.
  Changes that weaken them will not be accepted.
- Match the existing style: small functions, comments that explain *why*, and type hints.

## Code of conduct

Be respectful and constructive. Harassment or personal attacks are not tolerated in issues,
pull requests or discussions.
