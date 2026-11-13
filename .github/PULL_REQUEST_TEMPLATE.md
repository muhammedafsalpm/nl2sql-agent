## What and why

<!-- What does this change, and why is it needed? Link the issue if there is one. -->

## How it was tested

- [ ] `python -m compileall -q backend/app`
- [ ] `npm run build` in `frontend/`
- [ ] `python -m eval.run_eval` (if prompts, rules or SQL generation changed) — result:

## Checklist

- [ ] No secrets, `.env` files, real customer data or internal business rules are included
- [ ] README / `.env.example` updated if a setting was added or changed
- [ ] The SELECT-only / read-only guarantees are unchanged
