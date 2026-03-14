"""
openclaw-monitor  –  api/index.py
Vercel Python serverless entry-point (Flask / WSGI).

Env vars required:
  GITHUB_TOKEN    – GitHub PAT (needs gist scope)
  GIST_ID         – ID of the GitHub Gist used as storage

Env vars optional:
  COLLECT_SECRET  – If set, /api/collect requires header X-Secret or ?secret=
  REPO            – Owner/repo slug (default: openclaw/openclaw)
  MAX_SNAPSHOTS   – How many snapshots to keep (default: 3)
"""

import json
import os
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GIST_ID        = os.environ.get("GIST_ID", "")
COLLECT_SECRET = os.environ.get("COLLECT_SECRET", "")
REPO           = os.environ.get("REPO", "openclaw/openclaw")
MAX_SNAPSHOTS  = int(os.environ.get("MAX_SNAPSHOTS", "3"))
GIST_FILENAME  = "snapshots.json"

BASE = f"https://api.github.com/repos/{REPO}"

URL_MAP = {
    "repo_meta":          f"{BASE}",
    "commits":            f"{BASE}/commits?per_page=30",
    "prs_open":           f"{BASE}/pulls?state=open&sort=updated&direction=desc&per_page=30",
    "prs_closed":         f"{BASE}/pulls?state=closed&sort=updated&direction=desc&per_page=30",
    "issues_open":        f"{BASE}/issues?state=open&sort=updated&direction=desc&per_page=30",
    "issues_closed":      f"{BASE}/issues?state=closed&sort=updated&direction=desc&per_page=20",
    "events":             f"{BASE}/events?per_page=30",
    "pr_review_comments": f"{BASE}/pulls/comments?sort=updated&direction=desc&per_page=30",
    "issue_comments":     f"{BASE}/issues/comments?sort=updated&direction=desc&per_page=30",
    "contributors":       f"{BASE}/contributors?per_page=20",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _gh_headers() -> dict:
    h = {
        "Accept":     "application/vnd.github+json",
        "User-Agent": "openclaw-monitor/1.0",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _fetch_one(item: tuple) -> tuple:
    """Fetch a single URL; returns (key, data). Never raises."""
    key, url = item
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=8)
        r.raise_for_status()
        return key, r.json()
    except Exception as exc:
        return key, {"_fetch_error": str(exc), "url": url}


def _fetch_all() -> dict:
    """Fetch all GitHub API URLs in parallel (thread-pool)."""
    results = {}
    with ThreadPoolExecutor(max_workers=len(URL_MAP)) as pool:
        futures = {pool.submit(_fetch_one, item): item[0] for item in URL_MAP.items()}
        for future in as_completed(futures):
            key, data = future.result()
            results[key] = data
    return results


# ---------------------------------------------------------------------------
# Gist storage
# ---------------------------------------------------------------------------
def _gist_read() -> dict:
    """Read the snapshots store from Gist. Returns {'snapshots': [...]}."""
    if not GIST_ID:
        return {"snapshots": []}
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gh_headers(),
            timeout=12,
        )
        if r.status_code != 200:
            return {"snapshots": [], "_read_error": f"HTTP {r.status_code}"}

        files = r.json().get("files", {})
        if GIST_FILENAME not in files:
            return {"snapshots": []}

        file_info = files[GIST_FILENAME]
        # Gist truncates large files – follow raw_url if needed
        if file_info.get("truncated"):
            raw = requests.get(
                file_info["raw_url"], headers=_gh_headers(), timeout=20
            )
            content = raw.text
        else:
            content = file_info.get("content", "")

        return json.loads(content) if content.strip() else {"snapshots": []}
    except Exception as exc:
        return {"snapshots": [], "_read_error": str(exc)}


def _gist_write(store: dict) -> tuple[bool, str]:
    """Overwrite the Gist file with `store`. Returns (ok, message)."""
    if not GIST_ID:
        return False, "GIST_ID env var not set"
    if not GITHUB_TOKEN:
        return False, "GITHUB_TOKEN env var not set"
    try:
        payload = {
            "files": {
                GIST_FILENAME: {
                    # compact JSON to save space / speed up transfers
                    "content": json.dumps(store, separators=(",", ":"))
                }
            }
        }
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={**_gh_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if r.status_code == 200:
            return True, "ok"
        return False, f"Gist PATCH {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/api/collect", methods=["GET", "POST"])
def collect():
    """
    Fetch fresh GitHub data, prepend as a snapshot, prune to MAX_SNAPSHOTS,
    and persist to Gist.

    Protect with COLLECT_SECRET env var (optional):
      - Header:  X-Secret: <value>
      - Query:   ?secret=<value>
    """
    if COLLECT_SECRET:
        provided = request.headers.get("X-Secret") or request.args.get("secret", "")
        if provided != COLLECT_SECRET:
            return jsonify({"error": "Unauthorized – wrong or missing secret"}), 401

    now = datetime.now(timezone.utc)
    ts  = now.strftime("%Y%m%dT%H%M%S")
    iso = now.isoformat()

    # 1. Parallel fetch
    data = _fetch_all()

    snapshot = {
        "snapshot_id":  ts,
        "collected_at": iso,
        "repo":         REPO,
        **data,
    }

    # 2. Read → prepend → trim → write
    store     = _gist_read()
    snapshots = store.get("snapshots", [])
    snapshots.insert(0, snapshot)
    snapshots = snapshots[:MAX_SNAPSHOTS]

    ok, msg = _gist_write({
        "snapshots":    snapshots,
        "last_updated": iso,
        "repo":         REPO,
    })

    status_code = 200 if ok else 500
    return jsonify({
        "status":           "ok" if ok else "error",
        "message":          msg,
        "snapshot_id":      ts,
        "total_snapshots":  len(snapshots),
        "collected_at":     iso,
    }), status_code


@app.route("/api/snapshots", methods=["GET"])
def get_snapshots():
    """
    Return all stored snapshots in one JSON payload.
    This is the endpoint for your LLM to call.

    Response shape:
    {
      "repo": "openclaw/openclaw",
      "last_updated": "<ISO>",
      "snapshots": [ { snapshot_0 }, { snapshot_1 }, { snapshot_2 } ],
      "_meta": { "count": 3, "max_snapshots": 3, "served_at": "<ISO>" }
    }
    """
    store = _gist_read()
    store.setdefault("snapshots", [])
    store["_meta"] = {
        "count":         len(store["snapshots"]),
        "max_snapshots": MAX_SNAPSHOTS,
        "served_at":     datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(store)


@app.route("/api/status", methods=["GET"])
def status():
    """Quick health-check; shows config (no secrets)."""
    store = _gist_read()
    snapshots = store.get("snapshots", [])
    return jsonify({
        "ok":             True,
        "repo":           REPO,
        "max_snapshots":  MAX_SNAPSHOTS,
        "gist_id_set":    bool(GIST_ID),
        "token_set":      bool(GITHUB_TOKEN),
        "secret_set":     bool(COLLECT_SECRET),
        "snapshots_count": len(snapshots),
        "snapshot_ids":   [s.get("snapshot_id") for s in snapshots],
        "last_updated":   store.get("last_updated"),
    })


@app.route("/", methods=["GET"])
@app.route("/api", methods=["GET"])
def root():
    return jsonify({
        "service":     "openclaw-monitor",
        "version":     "1.0.0",
        "repo":        REPO,
        "description": "GitHub activity snapshots — last 3 kept (sliding window)",
        "endpoints": {
            "GET  /api/snapshots":  "All snapshots — call this from your LLM",
            "GET  /api/status":     "Health-check / config summary",
            "POST /api/collect":    "Trigger a fresh collection (cron target)",
            "GET  /api/collect":    "Same as POST (handy for browser / curl tests)",
        },
    })


# ---------------------------------------------------------------------------
# Local dev
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)
