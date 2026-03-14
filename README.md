# openclaw-monitor

A lightweight Vercel-hosted service that **collects GitHub activity snapshots** for a repo on a schedule, keeps a **sliding window of the last 3 snapshots** (~1.5 hrs), and exposes them through a **single public endpoint** so an LLM can fetch everything in one call.

```
GET /api/snapshots   →  { snapshots: [ {...}, {...}, {...} ] }
```

---

## How it works

```
Every 30 min (cron)
      │
      ▼
 /api/collect
      │
      ├─ Fetch 10 GitHub API endpoints IN PARALLEL
      │    commits · PRs · issues · events · comments · contributors · repo_meta
      │
      ├─ Read Gist  →  prepend new snapshot  →  trim to MAX_SNAPSHOTS (3)
      │
      └─ Write Gist

LLM
      │
      └─ GET /api/snapshots  →  all 3 snapshots in one JSON blob
```

Storage backend: **GitHub Gist** — free, no extra service needed, just a token.

---

## Quick-start

### 1 — Create a GitHub Gist (storage)

1. Go to <https://gist.github.com>
2. Create a **secret** gist with one file named **`snapshots.json`** containing exactly:
   ```json
   {}
   ```
3. Copy the **Gist ID** from the URL:
   ```
   https://gist.github.com/<your-user>/<GIST_ID>
   ```

### 2 — Create a GitHub Personal Access Token

Go to <https://github.com/settings/tokens/new>

Required scopes:
- **`gist`** — read + write snapshots
- No extra scopes needed (the repo being monitored is public)

### 3 — Deploy to Vercel

```bash
# Install Vercel CLI if needed
npm i -g vercel

git clone https://github.com/<you>/openclaw-monitor
cd openclaw-monitor

vercel          # follow prompts; choose Python runtime
```

Add environment variables in the Vercel dashboard (`Settings → Environment Variables`) or via CLI:

```bash
vercel env add GITHUB_TOKEN
vercel env add GIST_ID
vercel env add COLLECT_SECRET   # optional
```

### 4 — Set up a cron trigger

#### Option A — Vercel Cron (Pro plan required for 30-min interval)

Already configured in `vercel.json`:
```json
"crons": [{ "path": "/api/collect", "schedule": "*/30 * * * *" }]
```
Vercel Hobby only supports daily crons. If you're on Hobby, use Option B.

#### Option B — cron-job.org (free, recommended for Hobby plan)

1. Sign up at <https://cron-job.org> (free)
2. Create a job:
   - **URL**: `https://<your-app>.vercel.app/api/collect`
   - **Schedule**: Every 30 minutes
   - **Method**: GET
   - (If you set `COLLECT_SECRET`) add header `X-Secret: <your-secret>`

#### Option C — GitHub Actions (free)

Create `.github/workflows/collect.yml` in **any** repo you own:

```yaml
name: openclaw-monitor collect
on:
  schedule:
    - cron: "*/30 * * * *"
  workflow_dispatch:

jobs:
  collect:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger collection
        run: |
          curl -sf -X POST \
            -H "X-Secret: ${{ secrets.COLLECT_SECRET }}" \
            https://<your-app>.vercel.app/api/collect
```

Add `COLLECT_SECRET` as a repo secret.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/snapshots` | **Main endpoint.** Returns all stored snapshots. |
| `GET` | `/api/status` | Health-check — shows config, snapshot count, IDs. |
| `GET\|POST` | `/api/collect` | Trigger a fresh collection. Protected by `COLLECT_SECRET` if set. |
| `GET` | `/api` or `/` | Service info & endpoint listing. |

### `/api/snapshots` response shape

```jsonc
{
  "repo": "openclaw/openclaw",
  "last_updated": "2025-03-14T10:30:00+00:00",
  "snapshots": [
    {
      "snapshot_id":  "20250314T103000",
      "collected_at": "2025-03-14T10:30:00+00:00",
      "repo":         "openclaw/openclaw",
      "repo_meta":    { /* GitHub /repos/:owner/:repo */ },
      "commits":      [ /* last 30 commits */ ],
      "prs_open":     [ /* open PRs sorted by updated */ ],
      "prs_closed":   [ /* recently closed PRs */ ],
      "issues_open":  [ /* open issues */ ],
      "issues_closed":[ /* recently closed issues */ ],
      "events":       [ /* repo events */ ],
      "pr_review_comments": [ /* PR review comments */ ],
      "issue_comments":     [ /* issue comments */ ],
      "contributors": [ /* top contributors */ ]
    },
    { /* snapshot 30 min ago */ },
    { /* snapshot 60 min ago */ }
  ],
  "_meta": {
    "count": 3,
    "max_snapshots": 3,
    "served_at": "2025-03-14T10:35:12+00:00"
  }
}
```

### Using with an LLM (example prompt)

```
Fetch https://<your-app>.vercel.app/api/snapshots and analyze the 
openclaw/openclaw repository activity over the last 1.5 hours.
Identify: new commits, PR status changes, issue activity, and any 
notable patterns across the three snapshots.
```

---

## Local development

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your GITHUB_TOKEN and GIST_ID

python api/index.py              # runs on http://localhost:5000
```

Test collection:
```bash
curl http://localhost:5000/api/collect
curl http://localhost:5000/api/snapshots | python -m json.tool | head -60
```

---

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GITHUB_TOKEN` | **Yes** | — | PAT with `gist` scope |
| `GIST_ID` | **Yes** | — | Gist ID for snapshot storage |
| `COLLECT_SECRET` | No | (open) | Protect `/api/collect` |
| `REPO` | No | `openclaw/openclaw` | Repo to monitor |
| `MAX_SNAPSHOTS` | No | `3` | Sliding window size |

---

## Notes on Vercel + Python

- **Runtime**: `@vercel/python` — Vercel runs Flask via WSGI automatically when it finds `app = Flask(...)`.
- **Timeout**: `maxDuration: 60` is set in `vercel.json`; requires **Vercel Pro**. On Hobby the default is 10 s — enough if GitHub API responds quickly, but use an external cron (Option B/C) for reliability.
- **No persistent disk** — Vercel functions are stateless; all state lives in the Gist.
- **Cold starts** — The first request after inactivity may be ~1-2 s slower; negligible for a 30-min cron.
