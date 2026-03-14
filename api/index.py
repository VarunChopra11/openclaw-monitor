"""
openclaw-monitor  –  api/index.py
Vercel Python serverless entry-point (Flask / WSGI).

Collects SLIM GitHub data — only fields useful for contribution analysis.
Strips all noise: avatars, node_ids, PGP sigs, verification blobs, URLs, etc.

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
    "repo_meta":          BASE,
    "commits":            f"{BASE}/commits?per_page=30",
    "prs_open":           f"{BASE}/pulls?state=open&sort=updated&direction=desc&per_page=30",
    "prs_closed":         f"{BASE}/pulls?state=closed&sort=updated&direction=desc&per_page=20",
    "issues_open":        f"{BASE}/issues?state=open&sort=updated&direction=desc&per_page=30",
    "issues_closed":      f"{BASE}/issues?state=closed&sort=updated&direction=desc&per_page=20",
    "pr_review_comments": f"{BASE}/pulls/comments?sort=updated&direction=desc&per_page=30",
    "issue_comments":     f"{BASE}/issues/comments?sort=updated&direction=desc&per_page=30",
    "contributors":       f"{BASE}/contributors?per_page=20",
}

# ---------------------------------------------------------------------------
# Slim extractors — keep ONLY contribution-relevant fields
# ---------------------------------------------------------------------------

def _user(u: dict | None) -> str:
    """Return just the login string."""
    return u.get("login", "unknown") if u else "unknown"


def _labels(labels: list) -> list[str]:
    return [l.get("name", "") for l in labels if l.get("name")]


def _slim_commit(c: dict) -> dict:
    commit = c.get("commit", {})
    author = commit.get("author", {})
    return {
        "sha":     c.get("sha", "")[:10],
        "message": commit.get("message", "").split("\n")[0][:120],  # first line only
        "author":  author.get("name") or _user(c.get("author")),
        "date":    author.get("date", ""),
        "url":     c.get("html_url", ""),
    }


def _slim_issue(i: dict) -> dict:
    # GitHub issues endpoint returns PRs too — skip them
    if i.get("pull_request"):
        return None
    return {
        "number":     i.get("number"),
        "title":      i.get("title", ""),
        "state":      i.get("state", ""),
        "labels":     _labels(i.get("labels", [])),
        "author":     _user(i.get("user")),
        "assignees":  [_user(a) for a in i.get("assignees", [])],
        "comments":   i.get("comments", 0),
        "created_at": i.get("created_at", ""),
        "updated_at": i.get("updated_at", ""),
        "closed_at":  i.get("closed_at"),
        "body":       (i.get("body") or "")[:500],  # first 500 chars only
        "url":        i.get("html_url", ""),
    }


def _slim_pr(p: dict) -> dict:
    return {
        "number":      p.get("number"),
        "title":       p.get("title", ""),
        "state":       p.get("state", ""),
        "draft":       p.get("draft", False),
        "labels":      _labels(p.get("labels", [])),
        "author":      _user(p.get("user")),
        "assignees":   [_user(a) for a in p.get("assignees", [])],
        "reviewers":   [_user(r) for r in p.get("requested_reviewers", [])],
        "base_branch": p.get("base", {}).get("ref", ""),
        "head_branch": p.get("head", {}).get("ref", ""),
        "comments":    p.get("comments", 0),
        "commits":     p.get("commits", 0),
        "additions":   p.get("additions", 0),
        "deletions":   p.get("deletions", 0),
        "created_at":  p.get("created_at", ""),
        "updated_at":  p.get("updated_at", ""),
        "closed_at":   p.get("closed_at"),
        "merged_at":   p.get("merged_at"),
        "body":        (p.get("body") or "")[:400],
        "url":         p.get("html_url", ""),
    }


def _slim_comment(c: dict) -> dict:
    return {
        "id":         c.get("id"),
        "author":     _user(c.get("user")),
        "body":       (c.get("body") or "")[:300],
        "created_at": c.get("created_at", ""),
        "updated_at": c.get("updated_at", ""),
        "url":        c.get("html_url", ""),
        # issue_url tells us which issue/PR this belongs to
        "issue_url":  c.get("issue_url", "") or c.get("pull_request_url", ""),
    }


def _slim_contributor(c: dict) -> dict:
    return {
        "login":        c.get("login", ""),
        "contributions": c.get("contributions", 0),
    }


def _slim_repo_meta(r: dict) -> dict:
    return {
        "name":              r.get("full_name", ""),
        "description":       r.get("description", ""),
        "stars":             r.get("stargazers_count", 0),
        "forks":             r.get("forks_count", 0),
        "open_issues":       r.get("open_issues_count", 0),
        "language":          r.get("language", ""),
        "default_branch":    r.get("default_branch", ""),
        "topics":            r.get("topics", []),
        "license":           (r.get("license") or {}).get("spdx_id", ""),
        "updated_at":        r.get("updated_at", ""),
    }


def _slim(key: str, raw) -> object:
    """Dispatch raw API data through the right slim extractor."""
    if isinstance(raw, dict) and raw.get("_fetch_error"):
        return raw  # pass errors through unchanged

    if key == "repo_meta":
        return _slim_repo_meta(raw)

    if key == "commits":
        return [_slim_commit(c) for c in (raw if isinstance(raw, list) else [])]

    if key in ("prs_open", "prs_closed"):
        return [_slim_pr(p) for p in (raw if isinstance(raw, list) else [])]

    if key in ("issues_open", "issues_closed"):
        slimmed = [_slim_issue(i) for i in (raw if isinstance(raw, list) else [])]
        return [i for i in slimmed if i is not None]  # drop PRs mixed in

    if key in ("pr_review_comments", "issue_comments"):
        return [_slim_comment(c) for c in (raw if isinstance(raw, list) else [])]

    if key == "contributors":
        return [_slim_contributor(c) for c in (raw if isinstance(raw, list) else [])]

    return raw


# ---------------------------------------------------------------------------
# GitHub API fetcher
# ---------------------------------------------------------------------------
def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "openclaw-monitor/1.0"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _fetch_one(item: tuple) -> tuple:
    key, url = item
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=10)
        r.raise_for_status()
        return key, r.json()
    except Exception as exc:
        return key, {"_fetch_error": str(exc), "url": url}


def _fetch_all() -> dict:
    results = {}
    with ThreadPoolExecutor(max_workers=len(URL_MAP)) as pool:
        futures = {pool.submit(_fetch_one, item): item[0] for item in URL_MAP.items()}
        for future in as_completed(futures):
            key, raw = future.result()
            results[key] = _slim(key, raw)   # ← slim right here, before storing
    return results


# ---------------------------------------------------------------------------
# Gist storage
# ---------------------------------------------------------------------------
def _gist_read() -> dict:
    if not GIST_ID:
        return {"snapshots": []}
    try:
        r = requests.get(
            f"https://api.github.com/gists/{GIST_ID}",
            headers=_gh_headers(), timeout=12,
        )
        if r.status_code != 200:
            return {"snapshots": [], "_read_error": f"HTTP {r.status_code}"}

        files = r.json().get("files", {})
        if GIST_FILENAME not in files:
            return {"snapshots": []}

        file_info = files[GIST_FILENAME]
        if file_info.get("truncated"):
            raw = requests.get(file_info["raw_url"], headers=_gh_headers(), timeout=20)
            content = raw.text
        else:
            content = file_info.get("content", "")

        return json.loads(content) if content.strip() else {"snapshots": []}
    except Exception as exc:
        return {"snapshots": [], "_read_error": str(exc)}


def _gist_write(store: dict) -> tuple[bool, str]:
    if not GIST_ID:
        return False, "GIST_ID env var not set"
    if not GITHUB_TOKEN:
        return False, "GITHUB_TOKEN env var not set"
    try:
        payload = {
            "files": {
                GIST_FILENAME: {
                    "content": json.dumps(store, separators=(",", ":"))
                }
            }
        }
        r = requests.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={**_gh_headers(), "Content-Type": "application/json"},
            json=payload, timeout=30,
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
    if COLLECT_SECRET:
        provided = request.headers.get("X-Secret") or request.args.get("secret", "")
        if provided != COLLECT_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    now = datetime.now(timezone.utc)
    ts  = now.strftime("%Y%m%dT%H%M%S")
    iso = now.isoformat()

    data = _fetch_all()

    snapshot = {"snapshot_id": ts, "collected_at": iso, "repo": REPO, **data}

    store     = _gist_read()
    snapshots = store.get("snapshots", [])
    snapshots.insert(0, snapshot)
    snapshots = snapshots[:MAX_SNAPSHOTS]

    ok, msg = _gist_write({"snapshots": snapshots, "last_updated": iso, "repo": REPO})

    return jsonify({
        "status":          "ok" if ok else "error",
        "message":         msg,
        "snapshot_id":     ts,
        "total_snapshots": len(snapshots),
        "collected_at":    iso,
    }), 200 if ok else 500


@app.route("/api/snapshots", methods=["GET"])
def get_snapshots():
    """
    Main LLM endpoint. Returns all slim snapshots in one call.
    Data is already stripped to contribution-relevant fields only.
    """
    store = _gist_read()
    store.setdefault("snapshots", [])
    store["_meta"] = {
        "count":         len(store["snapshots"]),
        "max_snapshots": MAX_SNAPSHOTS,
        "served_at":     datetime.now(timezone.utc).isoformat(),
        "fields_kept": {
            "commits":   "sha(10), message(first line), author, date, url",
            "issues":    "number, title, state, labels, author, assignees, comments, body(500), dates, url",
            "prs":       "number, title, state, draft, labels, author, reviewers, branches, stats, body(400), dates, url",
            "comments":  "id, author, body(300), dates, issue_url, url",
            "repo_meta": "name, description, stars, forks, open_issues, language, topics, license",
        }
    }
    return jsonify(store)


@app.route("/api/status", methods=["GET"])
def status():
    store     = _gist_read()
    snapshots = store.get("snapshots", [])
    return jsonify({
        "ok":              True,
        "repo":            REPO,
        "max_snapshots":   MAX_SNAPSHOTS,
        "gist_id_set":     bool(GIST_ID),
        "token_set":       bool(GITHUB_TOKEN),
        "secret_set":      bool(COLLECT_SECRET),
        "snapshots_count": len(snapshots),
        "snapshot_ids":    [s.get("snapshot_id") for s in snapshots],
        "last_updated":    store.get("last_updated"),
    })


@app.route("/", methods=["GET"])
@app.route("/api", methods=["GET"])
def root():
    return jsonify({
        "service":     "openclaw-monitor",
        "version":     "2.0.0",
        "repo":        REPO,
        "description": "Slim GitHub activity snapshots for contribution analysis",
        "endpoints": {
            "GET  /api/snapshots": "All snapshots (slim) — call this from your LLM",
            "GET  /api/status":    "Health-check",
            "POST /api/collect":   "Trigger fresh collection (cron target)",
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)