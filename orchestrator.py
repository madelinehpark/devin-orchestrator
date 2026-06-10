"""Event-driven remediation orchestrator.

Polls a GitHub repo for open issues labeled `auto-fix`, dispatches a Devin
session per issue, tracks each session to a terminal state, and records
results to state/results.json (which the dashboard reads).

Run modes:
  DEVIN_MODE=mock (default)  — no network calls to Devin
  DEVIN_MODE=real            — live Devin API (costs ACUs!)
  GITHUB_TOKEN unset         — canned mock issues instead of live polling
  RUN_ONCE=1                 — one poll cycle, wait for in-flight sessions, exit
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from devin_client import DevinClient, client_from_env
from github_client import Issue, IssueSource, issue_source_from_env

logger = logging.getLogger("orchestrator")

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
PROCESSED_PATH = STATE_DIR / "processed.json"
RESULTS_PATH = STATE_DIR / "results.json"

PROMPT_TEMPLATE = (
    "Remediate the GitHub issue at {issue_url}. "
    "Follow its acceptance criteria exactly. "
    "Open a pull request and return its URL and a one-line summary "
    "in the structured output."
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env_file(path: str = ".env") -> None:
    """Tiny .env loader (KEY=VALUE lines; '#' comments). No dependency needed."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def select_new_issues(issues: list[Issue], processed: set[int]) -> list[Issue]:
    """Issues not yet dispatched. Pure function — unit tested."""
    return [issue for issue in issues if issue.number not in processed]


class StateStore:
    """Thread-safe persistence for processed issue numbers and results."""

    def __init__(self, processed_path: Path = PROCESSED_PATH, results_path: Path = RESULTS_PATH) -> None:
        self.processed_path = processed_path
        self.results_path = results_path
        self._lock = threading.Lock()
        self.processed_path.parent.mkdir(parents=True, exist_ok=True)
        self.processed: set[int] = set(self._read_json(self.processed_path, []))
        self.results: list[dict] = self._read_json(self.results_path, [])

    @staticmethod
    def _read_json(path: Path, default):
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    @staticmethod
    def _write_json(path: Path, data) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)

    def mark_processed(self, issue_number: int) -> None:
        with self._lock:
            self.processed.add(issue_number)
            self._write_json(self.processed_path, sorted(self.processed))

    def upsert_result(self, record: dict) -> None:
        """Insert or update the record keyed by issue_number."""
        with self._lock:
            for i, existing in enumerate(self.results):
                if existing["issue_number"] == record["issue_number"]:
                    self.results[i] = record
                    break
            else:
                self.results.append(record)
            self._write_json(self.results_path, self.results)


class Orchestrator:
    def __init__(self, devin: DevinClient, issues: IssueSource, store: StateStore, poll_interval: int = 30) -> None:
        self.devin = devin
        self.issues = issues
        self.store = store
        self.poll_interval = poll_interval
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    # ---- per-issue lifecycle -------------------------------------------------

    def dispatch(self, issue: Issue) -> None:
        record = {
            "issue_number": issue.number,
            "issue_title": issue.title,
            "session_id": None,
            "status": "dispatching",
            "pr_url": None,
            "summary": None,
            "acus_consumed": None,
            "started_at": utcnow(),
            "finished_at": None,
        }
        logger.info("DISPATCH issue #%s — %s", issue.number, issue.title)
        try:
            session_id = self.devin.create_session(PROMPT_TEMPLATE.format(issue_url=issue.html_url))
        except Exception:
            logger.exception("FAILED to create session for issue #%s", issue.number)
            record.update(status="failed", finished_at=utcnow(), summary="session creation failed (see logs)")
            self.store.upsert_result(record)
            self.store.mark_processed(issue.number)
            return

        record.update(session_id=session_id, status="running")
        self.store.upsert_result(record)
        self.store.mark_processed(issue.number)  # never dispatch the same issue twice
        logger.info("RUNNING issue #%s in session %s", issue.number, session_id)

        try:
            final = self.devin.poll_until_done(session_id)
            output = final.structured_output or {}
            # A session that delivered its structured output idles at
            # status="running" — record that as finished.
            outcome = "finished" if output else final.status
            record.update(
                status=outcome,
                pr_url=output.get("pr_url") or None,
                summary=output.get("summary"),
                acus_consumed=final.acus_consumed,
                finished_at=utcnow(),
            )
            logger.info("%s issue #%s — pr=%s acus=%s", outcome.upper(), issue.number, record["pr_url"], final.acus_consumed)
        except Exception:
            logger.exception("FAILED while polling session %s (issue #%s)", session_id, issue.number)
            record.update(status="failed", finished_at=utcnow(), summary="polling failed (see logs)")
        self.store.upsert_result(record)

    # ---- main loop -----------------------------------------------------------

    def poll_once(self) -> int:
        """One fetch-and-dispatch cycle. Returns how many issues were dispatched."""
        try:
            labeled = self.issues.fetch_labeled_issues()
        except Exception:
            logger.exception("FAILED to fetch issues; will retry next cycle")
            return 0
        fresh = select_new_issues(labeled, self.store.processed)
        for issue in fresh:
            thread = threading.Thread(target=self.dispatch, args=(issue,), name=f"issue-{issue.number}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return len(fresh)

    def run(self, run_once: bool = False) -> None:
        logger.info(
            "orchestrator up — devin=%s issues=%s poll_interval=%ss",
            type(self.devin).__name__,
            type(self.issues).__name__,
            self.poll_interval,
        )
        while not self._stop.is_set():
            dispatched = self.poll_once()
            if dispatched:
                logger.info("dispatched %d new issue(s)", dispatched)
            if run_once:
                break
            self._stop.wait(self.poll_interval)
        for thread in self._threads:
            thread.join()
        logger.info("orchestrator stopped — %d result(s) in %s", len(self.store.results), self.store.results_path)

    def stop(self, *_args) -> None:
        logger.info("shutdown requested; waiting for in-flight sessions…")
        self._stop.set()


def main() -> None:
    load_env_file()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    orchestrator = Orchestrator(
        devin=client_from_env(),
        issues=issue_source_from_env(),
        store=StateStore(),
        poll_interval=int(os.environ.get("POLL_INTERVAL", "30")),
    )
    signal.signal(signal.SIGTERM, orchestrator.stop)
    signal.signal(signal.SIGINT, orchestrator.stop)
    orchestrator.run(run_once=os.environ.get("RUN_ONCE", "") == "1")


if __name__ == "__main__":
    main()
