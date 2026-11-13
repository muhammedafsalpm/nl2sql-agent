# SQL Assistant — natural language to SQL for Microsoft SQL Server

An open-source **multi-agent NL-to-SQL assistant** built on LangGraph. Point it at any Microsoft
SQL Server database, describe your business in two plain text files, and your users can ask
questions in everyday language. It plans, writes T-SQL, runs it **read-only (SELECT only)**,
analyses and predicts, independently verifies its own answer, and draws a chart when asked —
from a terminal, an HTTP API, or a web chat UI ([frontend/](frontend/)) with optional voice.

**Nothing in the code is tied to one company or industry.** Everything specific to your data
lives in three places you control:

| What | Where |
|---|---|
| Connection, assistant name, one-line description of your data | `backend/.env` |
| Business rules: terms, table grain, tricky joins, status codes | `backend/domain/business_rules.md` |
| Example questions with their correct SQL | `backend/domain/few_shots.md` |

The tables, columns, keys and real lookup values are read from the live database automatically.
A complete working example for Microsoft's free **AdventureWorks** sample database is in
[examples/adventureworks/](examples/adventureworks/) — the quickest way to try it.

The LLM provider is configurable (`LLM_PROVIDER` in `.env`): **OpenAI** (e.g. `gpt-5-mini` for
reasoning and SQL, with an optional cheap `OPENAI_FAST_MODEL` such as `gpt-4o-mini` for routing)
or a **local** model through Ollama (e.g. Qwen3). The Orchestrator uses the cheap tier in
**Normal** mode only; in **Deep Search** it routes on the main model, which is better at judging
whether a question is a follow-up.

## Quickstart (about 10 minutes)

1. Restore the AdventureWorks sample into SQL Server and create a read-only login —
   [examples/adventureworks/README.md](examples/adventureworks/README.md) has the exact steps.
2. Set up the backend:
   ```bash
   cd backend
   python -m venv .venv
   .venv\Scripts\activate            # Windows   (macOS/Linux: source .venv/bin/activate)
   pip install -r requirements.txt
   copy .env.example .env             # macOS/Linux: cp .env.example .env
   ```
3. In `backend/.env` set your LLM key, the database connection, and the AdventureWorks block from
   the example's README (`DOMAIN_DIR=../examples/adventureworks`, `ALLOWED_SCHEMAS=...`).
4. Check everything is wired up, then start:
   ```bash
   python scripts/check_setup.py
   python serve.py
   ```
5. In a second terminal:
   ```bash
   cd frontend
   npm install
   npm run dev                        # open http://localhost:5173
   ```

To use **your own database**, see [Use your own database](#use-your-own-database).

## How it works — agents vs. nodes

A LangGraph state machine coordinates **11 steps**. **7 are LLM agents** that reason about the
question or the data. **4 are deterministic code, with no AI involved** — the safety check, the
database call, and the chart builder always behave the same way for the same input, on purpose.

**🤖 Agents (use the LLM)**

| Agent | Role |
|---|---|
| **Orchestrator** | Understands the question: route (data / clarify / chitchat), whether analysis is needed, whether a chart was asked for, whether it needs several queries |
| **Planner** | ReAct controller — for multi-part questions, decides "fetch more" or "answer now" |
| **SQL Author** | Writes one read-only T-SQL `SELECT` (knows the schema, business rules, few-shots) |
| **Analysis** | Calculations, rankings, risk scoring, predictions — only when the question needs it |
| **Verifier** | Independent fact-checker — runs BEFORE the answer is written; recalculates the numbers, checks the schema and business rules against the retrieved data (and Analysis's findings, if any); a rejection sends real feedback back to the SQL Author, not a reword request |
| **Synthesizer** | Writes the final, plain-language answer — once, from data the Verifier already approved |
| **Chitchat** | Replies to greetings / "what can you do" — never touches the database |

**⚙️ Nodes (pure code — no AI)**

| Node | Role |
|---|---|
| **Validator** | Hard SELECT-only safety gate: blocks INSERT/UPDATE/DELETE/DDL/EXEC/`sp_`/`xp_`/`OPENROWSET`/`OPENQUERY`/multi-statement — ignoring any keyword that appears only inside a quoted string, since data is not a command |
| **Executor** | Runs the approved query read-only, capped at `MAX_ROWS`, with a timeout |
| **Chart** | Builds a render-ready chart straight from the executed query's own rows — only when the user asked for one, so a chart can never disagree with the answer text |
| **Clarify** | Sends the Orchestrator's clarifying question straight back — no LLM call, so it can never be reworded into something different |

Key loops — every one is bounded, so a turn always finishes:
- **Self-correction** — a query rejected by the Validator or failing in the database is fed back
  to the SQL Author and rewritten, up to `MAX_SQL_RETRIES`.
- **Verification retry** — separately, the Verifier can send the SQL Author back to rewrite a
  query whose *data* doesn't support answering the question, up to `VERIFY_RETRIES`. This budget
  is deliberately **separate**: sharing one counter let unrelated syntax errors earlier in a turn
  leave the Verifier with no rewrites available.
- **Repeat guard** — if the SQL Author re-emits a query already run this turn, the graph
  short-circuits straight to the answer. Identical SQL returns identical data, so re-validating,
  re-executing and re-verifying it cannot change the verdict — it only burned time (72s observed).
- **Multi-query (ReAct)** — the Planner can gather data across several queries (up to
  `MAX_QUERIES`) for multi-part questions.

**Rejected data is never silently discarded.** If verification fails and the retry budget is
spent, the retrieved rows are kept and the answer is written from them **with a mandatory caveat**
telling the user the figures could not be fully verified. `ChatResponse.verified` (and the `done`
SSE event) carry the same fact as a boolean, so a client can flag it without parsing prose.

The Verifier runs *before* the Synthesizer, not after. That's deliberate: it means a rejection can
trigger an actual fix (new SQL, new data), not just a reworded answer built on data that was
already wrong — which the Synthesizer would have no way to truly correct. The trade-off: nothing
independently re-checks the Synthesizer's finished wording afterward, so the Synthesizer runs
exactly **once**, as the last reasoning step before the answer goes out.

## The flow, start to finish

```
question
   │
   ▼
Orchestrator ──clarify──► Clarify ─────────────────────────────► answer
   │         ──chitchat─► Chitchat ────────────────────────────► answer
   │
   ▼ (data question)
[Planner] ◄─────────────────────────────────┐   (loops back only if multi_step)
   │                                        │
   ▼                                        │
SQL Author ──► Validator ──► Executor ──────┘
   ▲              │              │
   └───retry──────┴──────retry───┘   (validation/DB error, up to MAX_SQL_RETRIES)
   │                              │
   │                 needs_analysis? ──yes──► Analysis
   │                              │                │
   │                              └────────┬───────┘
   │                                       ▼
   │                                  Verifier
   └──────────retry (data doesn't ───────┤
              support the answer,     approved
              same MAX_SQL_RETRIES)       │
                                          ▼
                                    Synthesizer   ← runs once, final
                                          │
                             wants_chart? ┤
                              yes │        │ no
                                  ▼        ▼
                               Chart     answer
                                  │
                                  ▼
                               answer
```

In plain words: the **Orchestrator** looks at the question first and decides what kind it is. For a
real data question, the **SQL Author** writes one query, the **Validator** checks it's safe, and
the **Executor** runs it. If the question needs deeper reasoning, **Analysis** runs next; otherwise
it's skipped. Then the **Verifier** independently checks the retrieved data (and Analysis's
findings, if any) against the original question — if the data doesn't actually support answering
it, that goes back to the **SQL Author** for a real rewrite, not a reword. Once the Verifier is
satisfied, the **Synthesizer** writes the final answer exactly once. If a chart was asked for, the
**Chart** step builds it from the same rows the Executor returned — never from numbers an LLM
re-typed. Every retry loop has a hard limit, so the turn always finishes.

## Prerequisites

- **Python 3.10+**
- **An LLM provider** — set with `LLM_PROVIDER` in `.env`:
  - `openai` — needs `OPENAI_API_KEY`; no local model to install. `OPENAI_MODEL` is the main
    reasoning/SQL model (and routing in Deep Search); `OPENAI_FAST_MODEL` is an optional cheap
    tier for the Planner, Chitchat and all of Normal mode.
  - `local` — Qwen3 (or any pulled model) via **Ollama**, running locally:
    ```
    ollama pull qwen3.6:35b-a3b
    ollama list
    ```
- **Microsoft ODBC Driver 18 (or 17) for SQL Server** installed —
  [download](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server).
  Windows ships an old built-in driver called just "SQL Server"; it is used only as a last resort
  and does not support modern encryption settings, so install 18. `python scripts/check_setup.py`
  shows which driver is in use.
- A **Microsoft SQL Server** database (2016 or later, including Azure SQL) and a **read-only**
  login for it.
- **Node 18+** — only for the web UI (`frontend/`). The backend and CLI do not need it.

## Setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
# copy .env.example to .env and fill in the database and model settings
```

> This installs the voice packages too (~183 MB). Voice stays **inactive** until you also install
> ffmpeg and run `python scripts/setup_voice.py` — see [Voice](#voice-optional-speak-a-question-hear-the-answer).
> Until then the mic simply never appears and nothing else is affected.

## Run

All backend commands run from the **`backend/`** directory.

```bash
python main.py
```

Type questions, e.g. (with the AdventureWorks example):
- `how many orders are there?`
- `show revenue by product category`
- `which orders shipped after their due date?`
- `predict which product categories will grow next year and explain why`

Commands: `reset` clears the conversation; `exit` / `quit` leaves.
Each answer shows the **SQL that was generated** and the response.

## HTTP API

The same agent graph, prompts, guards and DB layer are exposed over HTTP. The terminal CLI
(`python main.py`) still works unchanged.

```bash
cd backend
python serve.py                 # or: uvicorn app.api.main:app --port 8000
```

Interactive docs: **http://localhost:8000/docs**

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | Ask a question. `{question, history?, include_sql?}` |
| `POST` | `/api/chat/stream` | Same, as Server-Sent Events (progress + final answer) |
| `POST` | `/api/export` | Download an answer's data as Excel or Word |
| `GET` | `/api/health` | Liveness (always 200 while serving) |
| `GET` | `/api/ready` | Readiness — DB reachable, schema loaded (503 if not) |
| `GET` | `/api/info` | Non-secret config: provider, model, database, limits |
| `GET` | `/api/voice/status` | Whether voice is available (503-free; never loads a model) |
| `POST` | `/api/voice/transcribe` | Speech → text. Raw audio body, returns `{text}` |
| `POST` | `/api/voice/synthesize` | Text → speech. `{text}` in, WAV bytes out |

> The three `/api/voice/*` routes exist only when the optional voice packages are installed —
> see [Voice](#voice-optional-speak-a-question-hear-the-answer). Without them they report
> unavailable and nothing else changes.

### Example

```bash
# one-off question
curl -X POST http://localhost:8000/api/chat   -H "Content-Type: application/json"   -d '{"question":"how many orders are there?"}'
```

The API is **stateless**. For a follow-up, the client replays the previous turns:

```json
{
  "question": "now list 5 of them",
  "history": [
    {"user": "how many orders are there?", "assistant": "There are 31,465 orders."}
  ]
}
```

`include_sql` defaults to **false** so end users never see SQL or raw rows; set it to `true`
for a developer/admin view.

### Export (download an answer's data)

Every chat response that retrieved data carries an **`export_token`** — an opaque, signed handle
for the query behind that answer. Post it to `/api/export` to download the data as a file:

```bash
curl -X POST http://localhost:8000/api/export \
  -H "Content-Type: application/json" \
  -d '{"token":"<export_token from the chat response>","format":"xlsx"}' \
  -OJ
```

| Field | Meaning |
|---|---|
| `token` | The `export_token` from that answer. Required. |
| `format` | `xlsx` \| `docx` (default `xlsx`) |
| `question` | Optional — used for the file name and document title |
| `answer` | Optional — embedded as a preamble in `docx`; ignored for `xlsx` |
| `verified` | Pass the response's `verified` flag; `false` stamps an "indicative" note in the file |

**The file contains every row — `MAX_ROWS` does NOT apply.** That deliberate difference is the
whole point: `MAX_ROWS` limits how many rows reach the *LLM prompt* (a token-cost control), which
is why a chat answer says *"showing the first 20 of 40"*. An export has no LLM in it, so it
re-reads the full result. Ask for 40 rows and the chat shows 20 while the file holds all 40.

`export_token` is `null` on `chitchat` and `clarify` replies and on any turn that retrieved
nothing — so a client can gate an Export button on its presence alone, with no route check. It is
also sent regardless of `include_sql`, since the token is opaque and reveals no internals.

**Why a token instead of just posting SQL.** The chat pipeline only ever runs SQL its own SQL
Author wrote, against a schema that hides `EXCLUDED_TABLES` / `EXCLUDED_COLUMNS`. If this endpoint
took raw SQL, that guarantee would be gone — the SELECT-only validator blocks writes but does not
restrict *which* tables may be read, so a caller could pull a deliberately-hidden table. The token
is HMAC-signed with `EXPORT_SECRET`: a valid signature proves this server authored that exact SQL,
and anything forged or tampered with is refused with a `400`. Signing is stateless (no server-side
token store), so it works unchanged across multiple workers.

**The two formats differ by intent, not just extension:**

- **`xlsx`** — the data. Every row, up to `EXPORT_MAX_ROWS` and the `.xlsx` format's own
  1,048,576-row sheet limit. This is the format for "give me everything".
- **`docx`** — a readable document: the answer's narrative, then the data as a real Word table.
  Capped at 5,000 rows because it is a document format, not a data format.

Any cap that is hit is always disclosed in the file itself, never silently applied.

The Word narrative is the answer's **prose only**. The Synthesizer usually formats results as a
markdown table, and that table is deliberately stripped from the preamble — the document renders
the same rows properly as a Word table immediately below, so keeping both would print every
result twice (once as unreadable pipe-separated text). Headings, bullets and numbered lists are
converted to real Word styles, and the answer's "you can download this using the Export option"
line is dropped, since inside the downloaded file it has nothing to point at.

### Configuration

| Env var | Default | Meaning |
|---|---|---|
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | Bind address |
| `API_RELOAD` | `false` | Auto-reload (development only) |
| `API_KEY` | *(blank)* | Blank = open (local dev). Set it and every request must send `X-API-Key` |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins for a browser UI |
| `EXPORT_MAX_ROWS` | `1000000` | Upper bound on rows written to an export file. Independent of `MAX_ROWS` |
| `EXPORT_QUERY_TIMEOUT` | `600` | Seconds. Export re-reads the full result, so it does not inherit `QUERY_TIMEOUT` |
| `EXPORT_TOKEN_TTL` | `86400` | Seconds a download link stays valid (24h) |
| `EXPORT_SECRET` | *(blank)* | Signs export links. **Set this** — blank means a random per-process key, which breaks links on restart and fails entirely when `API_WORKERS > 1`. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |

### Notes and limits

- **A turn takes ~20-50s** (the agent makes several sequential LLM calls). Use
  `/api/chat/stream` so the UI can show progress instead of appearing frozen.
- The graph is synchronous, so each turn runs in a **worker thread** - the event loop stays
  free and requests are handled concurrently.
- **Stateless** - no server-side session store, so you can run multiple workers or instances
  freely. The client owns the conversation and sends it in `history` (the last
  `HISTORY_TURNS` turns are used — default 3).
  Nothing is ever written to the analytics database, which stays read-only.
- Before exposing this beyond localhost: set `API_KEY`, restrict `CORS_ORIGINS`, put it behind
  TLS, and switch the database login to a **read-only** account.

## Web UI

A React + Vite front end in `frontend/`. It talks to the API only — no business logic lives in
the browser. Its title and heading come from `APP_NAME` in `backend/.env` (via `/api/info`), and
its starter questions are built from the tables in the connected database, so the UI needs no
edit or rebuild when you change either.

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173
```

The Vite dev server proxies `/api` to `http://localhost:8000`, so **start the backend first** and
you never hit CORS in development. Run both at once (two terminals):

```bash
cd backend && python serve.py
cd frontend && npm run dev
```

An **Export** control appears under any answer that returned data (never on small talk or a
clarifying question), offering Excel and Word. It downloads the **complete** result even
when the message itself shows a capped preview — see [Export](#export-download-an-answers-data).
When a result *is* capped, the answer says so and mentions the Export option; a complete answer
does not.

## Charts

Charts are **opt-in per question**. A normal data question returns text; a chart appears only when
the user asks for one — "show a **donut chart** of revenue by category", "**chart** the order
count by territory", "**plot** sales over time".

How it works, and why it cannot lie:

1. The **Orchestrator** sets `wants_chart` (and `chart_type`) from the user's own words. It is never
   inferred — no chart request, no chart. This costs no extra LLM call.
2. Because a chart needs a breakdown, the **SQL Author** is told to return one label column plus
   numeric columns and to `GROUP BY` — asking to "chart the order count" yields a count *by* a
   dimension, not a single scalar.
3. The **Chart node** builds the spec **deterministically from the executed query's rows** — no LLM
   call, ~1ms. Values in the chart are the same values the Executor returned, so a chart can never
   disagree with the answer text.

| Result shape | Chart |
|---|---|
| date column + numeric | line |
| category + one numeric | bar, or donut when asked (max 8 slices, rest grouped as "Others") |
| category + several numerics | grouped bar |
| a single number | KPI card |
| no numeric column, or a **truncated** result | **no chart** — the answer says so |

It refuses rather than misleads: a result capped at `MAX_ROWS` is never plotted, because a partial
chart looks authoritative. Caveats travel with the chart in `note` — `"top 8 of 34 shown"`,
`"3 non-numeric value(s) plotted as 0"` — and the UI always renders them.

`POST /api/chat` returns the chart alongside the answer, independent of `include_sql`:
(values below are illustrative)

```json
{
  "answer": "Bikes bring in most of the revenue (94.6M), followed by Components (11.8M)…",
  "chart": {
    "type": "donut",
    "title": "revenue by category",
    "labels": ["Bikes", "Components", "Clothing", "Accessories"],
    "series": [{"name": "revenue", "data": [94651172.7, 11802593.29, 2120542.52, 1272072.88]}],
    "note": ""
  }
}
```

`/api/chat/stream` emits it as a `chart` SSE event before `done`, so it can paint while the answer
is still arriving.

## Voice (optional) — speak a question, hear the answer

Press the mic, ask out loud, and the answer is read back. **Voice is a wrapper, not a pipeline
change:** speech becomes text, that text goes through the *exact same* `/api/chat/stream` flow
(orchestrator → SQL Author → Validator → Executor → Verifier → Synthesizer), and the finished
answer text is turned back into audio. Retrieval, SQL generation and answering are identical
whether the question was typed or spoken. A **typed** question is never spoken back.

```
🎤 → /api/voice/transcribe → text → the normal chat flow → answer → /api/voice/synthesize → 🔊
```

### Setup (one time)

The Python packages come with the normal install — `faster-whisper` and `piper-tts` are in
`requirements.txt` (~183 MB). Two things still cannot come from pip:

```bash
# 1. ffmpeg — decodes the browser's recording to 16 kHz mono WAV (a system tool, not pip)
winget install Gyan.FFmpeg        # Windows
# sudo apt-get install -y ffmpeg  # Debian/Ubuntu

# 2. the Piper voice file (~60 MB) + pre-fetch the speech model
#    run this FROM the backend/ folder
python scripts/setup_voice.py
```

> **Windows: open a NEW terminal after installing ffmpeg.** winget edits the PATH, and a shell
> that was already open keeps the old one — `ffmpeg -version` will still say "not recognized" and
> voice will report unavailable even though everything is installed correctly.

**Optional, NVIDIA GPU only** — CTranslate2 needs cuBLAS/cuDNN, which the driver alone does not
ship. These are ~2 GB, which is why they are not in `requirements.txt`:

```bash
pip install -r requirements-voice.txt     # nvidia-cublas-cu12 + nvidia-cudnn-cu12
```

Skipping them is safe: the code detects the missing CUDA runtime, **falls back to CPU by itself**
and logs one warning. Measured on an RTX 6000 Ada — **0.66 s** per utterance with them, **2.7 s**
without.

### Turning it off

Installing the packages does not force voice on. Until ffmpeg *and* the voice file are present,
`/api/voice/status` reports unavailable, the mic never renders, and every other endpoint behaves
exactly as before. `VOICE_ENABLED=0` does the same deliberately. Nothing in the chat pipeline is
aware voice exists either way, and no pinned version in `requirements.txt` moves.

### Models

| | Model | Size | Notes |
|---|---|---|---|
| Speech-to-Text | **faster-whisper** (CTranslate2) | 138 MB – 2.9 GB | Auto-downloads on first use |
| Text-to-Speech | **Piper** (voice set by `TTS_MODEL`) | ~60 MB each | Auto-downloads on first use |

### Changing the voice

Set `TTS_MODEL` in `.env` and restart the server — that is the whole procedure. If the voice is
not on disk yet it is downloaded automatically the first time it speaks (~60 MB, once).

| `TTS_MODEL` | Voice | Accent |
|---|---|---|
| `en_US-ryan-medium` *(default)* | male | US |
| `en_US-lessac-medium` | female | US |
| `en_GB-alan-medium` | male | British |
| `en_GB-cori-medium` | female | British |

```bash
# backend/.env
TTS_MODEL=en_GB-cori-medium
```

A name that is not in the list is rejected at `/api/voice/status` with the valid names listed,
rather than failing later with a missing-file path. To add another Piper voice, put its Hugging
Face path in `app/voice/voices.py` — that one catalogue is used by both the server and
`setup_voice.py`, so they can never disagree about which voices exist.

`python scripts/setup_voice.py` pre-downloads **all four**, so switching later needs only a
restart with no wait.

`STT_MODEL=auto` sizes the speech model from the hardware, so one build runs everywhere:

| Hardware | Model chosen |
|---|---|
| GPU ≥ 8 GB VRAM | `large-v3` |
| GPU ≥ 4 GB VRAM (e.g. RTX 3050 laptop) | `small` |
| No GPU | `base` |

Pin it explicitly with `STT_MODEL=small` if you prefer. faster-whisper was chosen over a
PyTorch-based ASR precisely because one code path serves GPU *and* CPU, its smaller sizes fit a
4 GB laptop card, and it installs 13 packages instead of ~70 (no torch).

To test in terminal use: curl http://localhost:8000/api/voice/status

### Requirements and limits

* **The microphone needs a secure page.** Browsers expose `getUserMedia` only over **HTTPS or on
  `localhost`**. Opening the app at `http://<lan-ip>:5173` silently disables the mic — everything
  else keeps working, and the UI says so rather than failing quietly. This is a browser rule, not
  something the code can bypass.
* **Domain vocabulary is hard for any general speech model.** Abbreviations, product codes and
  identifiers such as `SO-43659` or `BK-M68B-42` are frequently misheard. Check the transcript that
  appears as your question before trusting an answer to a spoken query.
* Long answers are split at sentence/clause boundaries before synthesis — `en_US-ryan-medium` is a
  fast voice that slurs a single over-long utterance, and answers here are often long lists.

## Evaluate accuracy

A golden set of your own questions lets you measure quality after any prompt/rule change:

```bash
python -m eval.run_eval
```

It prints the generated SQL, row counts, and a table-hit check per question. The questions come
from `golden.jsonl` in your domain folder (`DOMAIN_DIR`) — format in
[backend/domain/README.md](backend/domain/README.md).

## Logging

Every request is logged (rotating) to **`logs/chatbot.log`**: question, route, each generated SQL
+ attempt, validation rejections, DB errors, row counts, and latency. Set verbosity with
`LOG_LEVEL` in `.env`.

### Live agent trace (console)

The API server also prints a colour-coded line per agent as the pipeline runs, so you can watch
the orchestration instead of only uvicorn's HTTP lines:

```
10:14:44 Startup       API startup: schema loaded (21448 chars)
10:14:44 Question      Q: how many orders are there?
10:14:51 Orchestrator  route=sql needs_analysis=False multi_step=False
10:14:54 SQL Author    SQL: SELECT COUNT(*) AS order_count FROM Sales.SalesOrderHeader
10:14:56 Executor      exec ok: 1 rows in 1.62s
10:15:01 Verifier      verify: ok
10:15:01 Done          API turn done in 16.87s
```

| Agent | Colour | | Agent | Colour |
|---|---|---|---|---|
| Question | bold white | | Executor | green (red on error) |
| Orchestrator | cyan | | Analysis | blue |
| Planner | magenta | | Verifier | bright magenta |
| SQL Author | yellow | | Done | bold green |
| Validator | bold red | | | |

Controlled by **`LOG_CONSOLE`** — defaults to **on** for the API server and **off** for the CLI
(so the CLI keeps its clean panels). Set `LOG_CONSOLE=false` to silence it. The file log is
written either way.

### Langfuse tracing (optional, self-hosted)

The console trace and the per-turn token table are **live** views — they scroll past. Langfuse
adds the **historical** one: every turn stored as a searchable trace, with per-agent spans, the
exact prompt and reply, token counts, latency and computed cost, plus dashboards over time.

**Off by default.** Blank `LANGFUSE_PUBLIC_KEY` means no handler is attached, no network call is
made, and every code path is identical to before Langfuse existed — verified, not assumed. To
disable it later, blank the key and restart; there is nothing to revert.

| Env var | Meaning |
|---|---|
| `LANGFUSE_BASE_URL` | Your self-hosted instance, e.g. `http://localhost:3000` |
| `LANGFUSE_PUBLIC_KEY` | `pk-lf-…` from the Langfuse UI. **Blank = tracing off** |
| `LANGFUSE_SECRET_KEY` | `sk-lf-…` from the same screen |
| `LANGFUSE_TRACING_ENABLED` | Optional second switch honoured by the SDK; `false` disables tracing even with keys set |
| `LANGFUSE_SAMPLE_RATE` | Optional `0.0`–`1.0` if trace volume outgrows your disk |

#### Setting it up, step by step (assumes nothing — new Docker install is fine)

Langfuse is a separate service (Postgres + ClickHouse + Redis + MinIO + web + worker). It is
**not part of this repo** and is never pushed to it — you clone it fresh from its own official
GitHub project, run it with Docker Compose, and connect this chatbot to it with 3 lines in `.env`.
Nothing here is installed by hand; Docker downloads and runs all six containers for you.

**1. Prerequisites.** Docker Desktop, running (the whale icon in your system tray, not animating).
No Postgres/Redis/ClickHouse/S3 installs of your own — Langfuse brings its own, in containers.

**2. Clone Langfuse — in its own folder, next to this repo, not inside it:**
```bash
cd ..
git clone https://github.com/langfuse/langfuse.git
cd langfuse
```

**3. Generate secrets BEFORE starting anything.** The compose file ships with public, well-known
defaults (`mysecret`, `postgres`, `myredissecret`…) — leaving them is like leaving `admin/admin`.
Create a `.env` file in this `langfuse` folder (not this repo's `.env` — a separate one) with:

| Variable | Generate with |
|---|---|
| `NEXTAUTH_SECRET` | `openssl rand -base64 32` |
| `SALT` | `openssl rand -base64 32` |
| `ENCRYPTION_KEY` | `openssl rand -hex 32` (must be exactly 64 hex characters) |
| `POSTGRES_PASSWORD` | any long random string |
| `CLICKHOUSE_PASSWORD` | any long random string |
| `REDIS_AUTH` | any long random string |
| `MINIO_ROOT_PASSWORD` | any long random string |
| `LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY` | **same value as `MINIO_ROOT_PASSWORD`** |
| `LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY` | **same value as `MINIO_ROOT_PASSWORD`** |
| `LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY` | **same value as `MINIO_ROOT_PASSWORD`** |
| `DATABASE_URL` | `postgresql://postgres:<your POSTGRES_PASSWORD>@postgres:5432/postgres` |

⚠ **Two traps that fail silently, not loudly** — worth reading twice as a beginner:
- `DATABASE_URL` **embeds** the Postgres password as plain text inside the connection string. If
  you change `POSTGRES_PASSWORD` you must also update the password *inside* `DATABASE_URL`, or
  login to the database fails.
- All three `LANGFUSE_S3_*_SECRET_ACCESS_KEY` values must **exactly equal** `MINIO_ROOT_PASSWORD`.
  Miss this and the stack starts up looking healthy but silently fails to store trace events —
  no error, just an empty Traces page forever.

**4. Check for port conflicts before starting** (common if you already run other Docker projects):
```bash
netstat -ano | findstr ":3000 :6379 :9000"
```
If any of those are already in use, edit `docker-compose.yml` and change only the **host** side
of the port mapping (the number before the colon) — e.g. `3000:3000` → `3100:3000`. Never change
the number *after* the colon; that's how containers talk to each other internally. If you move
the UI's port, also add `NEXTAUTH_URL=http://localhost:<your port>` to `.env` — otherwise login
redirects silently break. (For example, if 3000/6379/9000 are taken, remap them to
3100/6380/9010.)

**5. Start it:**
```bash
docker compose up -d
```
First run pulls ~6 images and can take a few minutes. Check progress with:
```bash
docker compose logs -f langfuse-web
```
Wait for it to log `Ready`.

**6. Open the UI in your browser** at whatever port you're using — e.g. `http://localhost:3000`
(or `:3100` if you remapped it). Sign up with your own email/password — this creates an account
**on your own instance only**, nothing external or shared. Then create an **Organization**, then
a **Project** inside it (name it anything, e.g. "SQL Assistant").

**7. Get your API keys.** Inside the project: **Settings → API Keys → Create new API key.** This
generates two values — copy both immediately, the secret key is shown only once:
- `pk-lf-…` → this is your `LANGFUSE_PUBLIC_KEY`
- `sk-lf-…` → this is your `LANGFUSE_SECRET_KEY`

**8. Connect the chatbot.** In **this repo's** `backend/.env` (not the langfuse folder's), set:
```
LANGFUSE_BASE_URL=http://localhost:3000    # or :3100 if you remapped the port
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```
⚠ **If a blank `LANGFUSE_PUBLIC_KEY=` line already exists anywhere else in your `.env`, delete
it.** `.env` files load top-to-bottom and the *last* line for a given name wins — a leftover
blank line below your real key will silently override it with nothing, and tracing stays off with
no error at all. Keep exactly one line per variable. (This happened during the original setup and
took a moment to diagnose — the fix was consolidating to one definition per key.)

**9. Restart the chatbot backend** so it picks up the new `.env` values, then ask it a question.
Within a few seconds a trace should appear on the Langfuse **Tracing** page. If it doesn't, the
most likely cause is step 8's duplicate-key trap — check for a second definition of the same
variable name.

**10. Lock the front door.** Once your account exists, set `AUTH_DISABLE_SIGNUP=true` in the
Langfuse `.env` and run `docker compose up -d` again. Until you do this, **anyone who can reach
the UI port can register their own account and read every trace** — and traces from this pipeline
contain real business data (see the warning below).

> ⚠ **Traces contain the full prompt and reply of every LLM call** — for this pipeline that
> includes the schema, the business rules, and retrieved rows (employee names, phone numbers).
> Self-hosting keeps that on-premise, which is the point; still treat the Langfuse instance as
> holding production data, keep its ports bound to `127.0.0.1` (not `0.0.0.0`), and complete
> step 10 above before anyone else can reach it.

**Turning it off** at any time: blank `LANGFUSE_PUBLIC_KEY` in `backend/.env` and restart. No
code to revert — the app runs exactly as it did before Langfuse existed. This is verified
behaviour, not a claim: with the key blank, no network calls to Langfuse happen at all, and if you
stop the Langfuse containers entirely while the key IS set, the chatbot still answers normally
(tracing failures are caught and logged, never surfaced to a user).

**Where it hooks in** (3 files, all additive, all behind the blank-key switch):
`app/tracing.py` owns the enable decision; `runtime.py` adds the handler to the one `config` dict
that all three graph execution paths already share; `llm.py` names each span after the calling
agent using the label `_log_call` already derives, so a trace reads *orchestrator → sql_author →
analysis → verifier → synthesize* instead of five identical `ChatOpenAI` entries.

**Deploying this chatbot elsewhere?** The `langfuse` folder is never part of this repo and is
never pushed anywhere — each new server/person who wants tracing repeats steps 1–7 independently,
on their own machine, with their own fresh secrets and their own account (self-hosted Langfuse has
no shared/central login — each instance's accounts live only in that instance's own database).
Skipping Langfuse entirely (leaving the 3 keys blank) is a fully supported, zero-setup choice.

## Configuration (`.env`)

| Key | Meaning |
|-----|---------|
| `APP_NAME` | Display name in the UI, API docs, CLI and export file names (default `SQL Assistant`) |
| `APP_DESCRIPTION` | One sentence about what your database holds; given to the agents. Optional |
| `DOMAIN_DIR` | Folder with your `business_rules.md`, `few_shots.md`, `golden.jsonl` (default `domain`, relative to `backend/`) |
| `LLM_MODEL`, `OLLAMA_BASE_URL`, `OLLAMA_NUM_CTX` | Local model, Ollama endpoint, context window |
| `DB_SERVER`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` | Database connection |
| `DB_DRIVER` | Blank = auto-detect ODBC driver (18 → 17 → SQL Server) |
| `DB_ENCRYPT`, `DB_TRUST_CERT` | TLS options for the connection |
| `ALLOWED_SCHEMAS` | Schemas the bot may see/query. Blank = every user schema (`dbo` included) |
| `EXCLUDED_TABLES`, `EXCLUDED_COLUMNS` | Tables/columns hidden from the agents (columns named like password/token/secret are always hidden) |
| `MAX_ROWS`, `QUERY_TIMEOUT`, `MAX_SQL_RETRIES` | Row cap, query timeout, SQL retry budget (Validator/Executor failures) |
| `MAX_QUERIES` | Planner's multi-query budget |
| `VERIFY_RETRIES` | How many times a Verifier rejection may send the SQL Author back — its own budget, separate from `MAX_SQL_RETRIES` |
| `LLM_TIMEOUT`, `LLM_MAX_RETRIES` | Per-request LLM timeout and provider retry count — without a timeout a stalled call holds a worker thread and its DB connection indefinitely |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |
| `VOICE_ENABLED` | `0` turns voice off completely — endpoints report unavailable, mic hidden |
| `STT_MODEL` | `auto` sizes the speech model from the hardware; or pin `tiny`/`base`/`small`/`medium`/`large-v3` |
| `TTS_MODEL` | Piper voice: `en_US-ryan-medium` (default), `en_US-lessac-medium`, `en_GB-alan-medium`, `en_GB-cori-medium`. Downloaded automatically on first use |
| `VOICE_DEVICE` | `auto` \| `cuda` \| `cpu`. `auto` uses the GPU when usable and falls back to CPU by itself |
| `PIPER_DATA_DIR` | Blank = `backend/models/piper` (what `scripts/setup_voice.py` fills) |

## Use your own database

Database-specific knowledge is kept **out of the Python code**, so a new database is a config and
text-file change, never a code change.

| Layer | Where it lives | Automatic? |
|---|---|---|
| Tables, columns, types, PKs, FKs, duplicate-key warnings, value hints | introspected live from the DB | ✅ automatic |
| Connection, schemas in scope, hidden tables/columns, assistant name | `backend/.env` | edit |
| Business meaning (definitions, table grain, non-obvious joins) | `domain/business_rules.md` | edit |
| Worked question → SQL examples | `domain/few_shots.md` | edit |
| Accuracy test set | `domain/golden.jsonl` | edit |

### Steps

**1. Create a read-only login.** The assistant only ever runs SELECTs, but the login is the real
safety boundary — give it `db_datareader` and nothing else (plus `VIEW DATABASE STATE` if you want
new data noticed faster). See the example's README for the SQL.

**2. Point at the database — `backend/.env`**
```ini
APP_NAME=Sales Insights
APP_DESCRIPTION=Orders, customers, products and stock of an online shop
DB_SERVER=... / DB_NAME=... / DB_USER=... / DB_PASSWORD=...
ALLOWED_SCHEMAS=                 # blank = all user schemas; or e.g. sales,inventory
EXCLUDED_COLUMNS=customer.tax_id # anything personal or irrelevant the agents should not see
```
To see which schemas hold tables:
```sql
SELECT s.name, COUNT(t.object_id) AS tables FROM sys.schemas s
LEFT JOIN sys.tables t ON t.schema_id = s.schema_id
GROUP BY s.name HAVING COUNT(t.object_id) > 0 ORDER BY 2 DESC;
```

**3. Check the setup and read the schema**
```bash
python scripts/check_setup.py
python -m app.db.introspect
```
`introspect` clears `.cache/`, rebuilds from the live DB, and prints the table count and size. A
suspiciously small size means `ALLOWED_SCHEMAS` or the login's permissions are wrong.

> **New tables and columns are picked up automatically.** On every start the app runs one cheap
> catalogue query and compares a fingerprint of the live structure against the one the cache was
> built from. If anything is added, dropped, renamed or retyped — or you change
> `ALLOWED_SCHEMAS` — the cache rebuilds itself. You still need the command above when you point at
> a **different database**, or to refresh the **value hints** (real lookup values).

**4. Write your domain files** in `backend/domain/` — the templates there explain each section, and
[docs/writing-domain-files.md](docs/writing-domain-files.md) is the full guide. Introspection
sees structure but not *meaning*, so these carry what it cannot infer, for example:
- **table grain** — "order_lines has one row per product per order; count orders with
  `COUNT(DISTINCT order_id)`"
- **non-obvious joins** — "a customer's name is in `crm.person` OR `crm.company`"
- **business definitions** — "an *active* customer ordered in the last 12 months"

Run every example in `few_shots.md` against the database before adding it — a wrong example
teaches the model to be wrong.

> Empty domain files are fine: the assistant falls back to the generic rules plus the live
> schema. **An empty file is safer than a stale one**, which actively misleads the model.

**5. Add test questions** to `domain/golden.jsonl`, then run `python -m eval.run_eval`.

### Just refreshing the cache
If only data or a column changed, `python -m app.db.introspect` is enough (adding *rows* needs
nothing).

## Safety

- The app **only ever issues SELECT**. The Validator rejects any write/DDL/`EXEC`/multi-statement,
  plus read-side risks a plain SELECT-only check would miss — `OPENROWSET`/`OPENQUERY`/
  `OPENDATASOURCE`/`WAITFOR` and reads of `sys.sql_logins`/`sys.server_principals`. It scrubs
  string literals and comments before scanning, so a legitimate value like
  `WHERE remarks = 'update required'` is never mistaken for a command.
- ⚠️ **Always connect with a read-only SQL login** (`db_datareader` only), never `sa` or an
  owner account — that is the real safety boundary, whatever the validator catches.
- Only the schemas in `ALLOWED_SCHEMAS` are visible (blank = all user schemas; system schemas
  are never included). Tables/columns in `EXCLUDED_TABLES`/`EXCLUDED_COLUMNS`, and any column
  whose name looks secret (password/token/credential/secret), are hidden from the LLM entirely.
- Everything the agents read — the schema, your domain files and the retrieved rows — is sent to
  the configured LLM provider. Use a local model (`LLM_PROVIDER=local`) if your data must not
  leave your network.

## Project structure

```
sql-assistant/
├── examples/adventureworks/          # complete sample domain pack (rules, few-shots, tests)
├── docs/writing-domain-files.md      # how to write good business rules and examples
├── backend/                          # Python — agents, API, database
│   ├── main.py                       # entry point (terminal CLI)
│   ├── serve.py                      # entry point (HTTP API / uvicorn)
│   ├── requirements.txt / .env / .env.example
│   ├── requirements-voice.txt        # OPTIONAL GPU extras for voice (~2 GB CUDA runtime)
│   ├── scripts/check_setup.py        # checks driver, DB login, schemas, domain files, LLM key
│   ├── scripts/setup_voice.py        # one-time: ffmpeg check + download the Piper voice
│   ├── models/piper/                 # voice .onnx files (gitignored, ~60 MB)
│   ├── domain/                       # YOUR database knowledge (templates; DOMAIN_DIR in .env)
│   │   ├── business_rules.md         #   definitions, table grain, non-obvious joins
│   │   ├── few_shots.md              #   worked question -> SQL examples
│   │   └── golden.jsonl              #   (you create it) test questions for the eval
│   ├── eval/  run_eval.py
│   └── app/
│       ├── config.py                 # settings from .env
│       ├── llm.py                    # provider + fast/main model tiers
│       ├── utils.py                  # strip <think>, extract SQL/JSON
│       ├── observability.py          # file logging + colour agent trace
│       ├── tracing.py                # optional Langfuse tracing (inert unless keys are set)
│       ├── api/  main.py  runtime.py  schemas.py    # FastAPI app, singletons, request/response models
│       │         export.py  export_formats.py       # signed download tokens + Excel/Word writers
│       ├── db/   connection.py  introspect.py       # read-only connection + schema/value-hint introspection
│       ├── voice/                    # OPTIONAL — delete this folder to remove voice entirely
│       │   ├── config.py             #   VOICE_* settings + hardware-based model sizing
│       │   ├── service.py            #   STT/TTS, lazy singletons, GPU->CPU fallback
│       │   └── routes.py             #   /api/voice/{status,transcribe,synthesize}
│       └── graph/
│           ├── state.py  prompts.py  build.py
│           └── nodes/  orchestrator  planner  sql_author  validator  executor
│                       analysis  synthesize  verifier  chart  finalize
└── frontend/                         # React + Vite + TypeScript + ECharts
    ├── package.json  vite.config.ts  tsconfig.json  index.html
    └── src/
        ├── App.tsx  main.tsx  types.ts  styles.css
        ├── api/client.ts             # POST /api/chat/stream + manual SSE parsing, POST /api/export
        ├── components/  Chart  KpiCard  MessageBubble  Composer  SqlDrawer  ExportMenu
        └── voice/                    # OPTIONAL — delete this folder to remove voice entirely
            ├── voiceApi.ts           #   fetch client for the three /api/voice endpoints
            ├── useVoiceRecorder.ts   #   MediaRecorder + live mic levels + transcription
            ├── useSpeaker.ts         #   WAV playback with a stop control
            ├── MicButton.tsx         #   renders nothing unless the backend has voice
            └── voice.css             #   imported by MicButton, so styles.css is untouched
```

> `types.ts` mirrors `backend/app/api/schemas.py` by hand — change one, change the other.

## Notes

- Analysis/prediction is done by the **LLM** (no Python sandbox); exact counts/totals are pushed
  into SQL aggregates so the database computes them precisely.
- Supported database: **Microsoft SQL Server** (T-SQL) only. The prompts, validator and schema
  reader are written for its dialect and catalogue views.
- More agents per question means more LLM calls = slower on a local model; tune with
  `MAX_QUERIES` / `MAX_SQL_RETRIES`, or use **Normal** mode for simple questions.

## License

MIT — see [LICENSE](LICENSE).
