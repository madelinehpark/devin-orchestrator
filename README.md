# Devin Auto-Remediation Orchestrator

Event-driven pipeline: GitHub issues labeled **`auto-fix`** are dispatched to
[Devin](https://devin.ai) (Cognition's autonomous coding agent) via its REST API.
Each session is tracked to completion and the resulting pull request is reported
on a live dashboard.

```
GitHub issues (label: auto-fix)
        │  poll every POLL_INTERVAL
        ▼
  orchestrator.py ──► Devin API (one session per issue, ACU-capped)
        │                   │ poll w/ backoff until finished/blocked
        ▼                   ▼
  state/results.json ◄── structured output {pr_url, status, summary}
        │
        ▼
  dashboard (static HTML, auto-refreshing)
```

## Architecture

| File | Role |
|---|---|
| `devin_client.py` | `RealDevinClient` (Devin API v3, Bearer auth, ACU cap, structured output schema) + `MockDevinClient` (no network; happy / slow / blocked scenarios) behind one interface. `DEVIN_MODE=mock\|real`. |
| `github_client.py` | Lists open issues with the configured label via the GitHub REST API; PRs filtered out. `MockIssueSource` provides canned issues when no token is set. |
| `orchestrator.py` | Main loop: fetch labeled issues → skip already-processed (`state/processed.json`) → one Devin session per issue, tracked concurrently → results to `state/results.json`. |
| `dashboard/` | Single static page: per-issue status, PR links, totals. No framework. |

## Run in mock mode (zero credentials)

```bash
docker compose up --build
# dashboard: http://localhost:8080/dashboard/
```

Or without Docker:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python orchestrator.py            # mock devin + canned issues
python3 -m http.server 8080 &               # serve dashboard from repo root
open http://localhost:8080/dashboard/
```

Three canned issues run through the happy, slow, and blocked paths.

## Run real

```bash
cp .env.example .env   # fill in DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, GITHUB_REPO
# set DEVIN_MODE=real
docker compose up --build
```

Label an issue `auto-fix` in the target repo. The orchestrator picks it up within
`POLL_INTERVAL` seconds, opens a Devin session (hard-capped at `MAX_ACU_LIMIT` ACUs),
and the PR link appears on the dashboard when the session finishes.

Issues are never dispatched twice — processed numbers persist in `state/processed.json`.
To re-run an issue, delete its entry from that file.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/pytest
```

Covers the poller's dedupe logic and the mock client lifecycle.
