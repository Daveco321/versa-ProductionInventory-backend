# Versa Inventory Ledger — Backend

Flask + Postgres backend for the Versa Inventory Ledger frontend. Replaces the
browser-only localStorage storage so the ledger is shared company-wide,
survives device swaps, and supports multiple concurrent users.

## What's inside

```
versa-ledger-backend/
├── app.py                  # Flask app, all endpoints, SQLAlchemy models
├── requirements.txt        # Python deps (pinned)
├── Procfile                # Gunicorn start command for Render/Heroku
├── render.yaml             # Render Blueprint — one-click deploy
├── .env.example            # All env vars documented
├── .gitignore
└── README.md               # this file
```

## Architecture

```
Frontend (index.html)          Backend (this repo)            Existing platform
─────────────────────          ────────────────────            ─────────────────
   browser fetch                 Flask + Gunicorn               your old Flask app
       │                              │                              │
       ▼                              ▼                              ▼
  HTTPS to /ledger              Postgres (Render)              /inventory data
  HTTPS to /uploads             SQLAlchemy ORM                 /overrides data
  HTTPS to /ledger/reset        Audit trail in DB              proxied via /proxy/*
```

## Local development

You don't need Postgres to develop — SQLite works fine.

```bash
cd versa-ledger-backend
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then edit
python app.py
```

Backend will listen on `http://localhost:5000`. SQLite DB file is created at
`./versa_ledger.db` on first request — delete it to start fresh.

Smoke test:
```bash
curl http://localhost:5000/health
curl http://localhost:5000/ledger
```

## Production deploy — Render (recommended)

This is the easiest path because your existing platform already runs there.

### One-time setup

1. **Create a GitHub repo** for this folder, push the contents.
2. Go to <https://dashboard.render.com> → **New** → **Blueprint**.
3. Connect the GitHub repo. Render reads `render.yaml` and creates two services:
   - **versa-ledger-api** (the web service)
   - **versa-ledger-db** (the Postgres database)
4. After the database provisions, Render auto-injects `DATABASE_URL` into the
   web service's environment. No action needed from you.
5. **Set the secrets** in the web service's Environment tab:
   - `RESET_PASSWORD` — strong password for the reset endpoint (replaces `Versa1211`)
   - `API_BEARER_TOKEN` — optional but recommended; lock down all endpoints
   - `ALLOWED_ORIGINS` — your frontend domain(s); already pre-filled in `render.yaml`
6. Trigger a manual deploy. First boot creates the tables automatically (via
   `init_db()` at the bottom of `app.py`).

### Free tier caveats

The free tier is fine for evaluation but **not for production**:
- The web service sleeps after 15min idle. First request after sleep takes
  30-60s. Reconciliation will look like it's hanging during cold start.
- The Postgres free tier **expires after 90 days** with no recovery.
- Upgrade to Render's Starter plan ($7/mo per service = $14/mo total) for
  always-on + persistent Postgres.

### Verify it's live

```bash
curl https://YOUR-API.onrender.com/health
# → {"status":"ok","db":true,...}

curl https://YOUR-API.onrender.com/ledger
# → {"ledger":{},"seedInfo":{},"count":0}
```

## Updating the frontend

The frontend (`index.html`) has been updated to call this backend. Two things
to configure before going live:

1. **Set the backend URL in `index.html`**. Find the constant
   ```js
   const BACKEND_API_URL = ...
   ```
   near the top of the script section. The default is
   `https://versa-ledger-api.onrender.com` — change to your deployed URL.

2. **Set the bearer token** (only if you set `API_BEARER_TOKEN` on the backend).
   Find `BACKEND_API_TOKEN` in `index.html` and paste the same value.

The frontend talks to the backend on every page load (to hydrate state) and
on every upload commit / edit / delete. There's no localStorage usage anymore.

## Endpoints reference

| Method | Path | Body | Purpose |
|---|---|---|---|
| GET | `/health` | — | Liveness check + DB status |
| GET | `/ledger` | — | Full ledger + seed info |
| POST | `/ledger/seed` | `{rows:[...]}` | One-time seed (refuses if non-empty) |
| POST | `/ledger/reset` | `{password, rows:[...]}` | **Destructive** wipe + reseed |
| GET | `/uploads` | — | All uploads (no items) |
| GET | `/uploads/<id>` | — | Single upload + items |
| POST | `/uploads` | `{id, type, items, ...}` | Commit a parsed upload |
| PATCH | `/uploads/<id>` | `{items:[...]}` | Edit upload items |
| DELETE | `/uploads/<id>` | — | Soft-delete + reverse |
| GET | `/proxy/inventory` | — | Pass-through to existing /inventory |
| GET | `/proxy/overrides` | — | Pass-through to existing /overrides |

Quantity convention: **packing lists store positive quantities, invoice
reports store negative quantities.** The frontend already handles this.

## Database schema

Four tables. Schema is defined in `app.py` and auto-created on first boot.

- **ledger** — current quantity per base style (style is primary key)
- **uploads** — header row per upload; `deleted_at` for soft delete
- **upload_items** — denormalized line items, so reverts don't re-parse the file
- **seed_info** — singleton row tracking the last seed/reset action

Migrations: there are none yet. If you change the schema in `app.py`, drop the
DB and let `init_db()` recreate. For production schema evolution, swap in
Alembic — small enough lift to add later.

## Backup

Render's paid Postgres includes daily automatic backups. On the free tier you
get manual download from the dashboard. For real production, schedule a
nightly `pg_dump` to S3:

```bash
pg_dump $DATABASE_URL | gzip | aws s3 cp - s3://your-bucket/backups/$(date +%F).sql.gz
```

## Security TODOs (before going fully live)

The current setup is fine for an internal company tool but the following
hardening is recommended:

- [ ] Set `API_BEARER_TOKEN` so endpoints aren't world-accessible
- [ ] Replace the `require_auth` shim with real session/JWT/SSO auth that maps
      to actual user identities (so the audit trail records *who* uploaded what)
- [ ] Rotate `RESET_PASSWORD` from the default `Versa1211`
- [ ] Restrict `ALLOWED_ORIGINS` to your actual frontend domains (no `*`)
- [ ] Add an Alembic migration system before the first schema change
- [ ] Add per-user audit log table (which user did what, when)
- [ ] Add rate limiting (Flask-Limiter) on POST endpoints

## Troubleshooting

**Frontend can't reach backend → CORS error in console**
Your frontend domain isn't in `ALLOWED_ORIGINS`. Fix on Render → Environment
→ edit `ALLOWED_ORIGINS` → save → wait for redeploy.

**"already_seeded" error when trying to seed**
The DB already has rows. Use `/ledger/reset` (password-protected) to wipe.

**First request after deploy takes 60s**
Render free tier cold start. Upgrade to Starter plan ($7/mo) to keep the
service warm.

**`psycopg2.OperationalError: connection refused`**
DATABASE_URL points at the wrong Postgres instance, or the DB hasn't finished
provisioning yet. Wait 1-2 minutes, then redeploy.

**Lost the reset password**
SSH into Render's shell (or use the dashboard) → set a new RESET_PASSWORD env
var → trigger redeploy.

## Cost summary

Free tier (eval/staging): $0
Production (recommended): ~$14/mo
- Web service Starter: $7/mo (always-on, faster CPU)
- Postgres Starter: $7/mo (persistent, daily backups)

For 50+ users or 100k+ inventory rows, consider Standard tiers ($25+/mo each)
for better DB performance + connection limits.
