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

from devin_client import TERMINAL_STATUSES, DevinClient, client_from_env
from github_client import Issue, IssueSource, issue_source_from_env

logger = logging.getLogger("orchestrator")

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
PROCESSED_PATH = STATE_DIR / "processed.json"
RESULTS_PATH = STATE_DIR / "results.json"
EVENTS_PATH = STATE_DIR / "events.json"
MAX_EVENTS = 300

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

    def __init__(
        self,
        processed_path: Path = PROCESSED_PATH,
        results_path: Path = RESULTS_PATH,
        events_path: Path = EVENTS_PATH,
    ) -> None:
        self.processed_path = processed_path
        self.results_path = results_path
        self.events_path = events_path
        self._lock = threading.Lock()
        self.processed_path.parent.mkdir(parents=True, exist_ok=True)
        self.processed: set[int] = set(self._read_json(self.processed_path, []))
        self.results: list[dict] = self._read_json(self.results_path, [])
        self.events: list[dict] = self._read_json(self.events_path, [])

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

    def append_event(self, message: str, issue_number: int = None, kind: str = "info") -> None:
        """Activity-feed entry, mirrored to the dashboard."""
        with self._lock:
            self.events.append({"at": utcnow(), "issue": issue_number, "kind": kind, "message": message})
            self.events = self.events[-MAX_EVENTS:]
            self._write_json(self.events_path, self.events)

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
            "session_url": None,
            "status": "dispatching",
            "status_detail": None,
            "pr_url": None,
            "summary": None,
            "acus_consumed": None,
            "started_at": utcnow(),
            "finished_at": None,
        }
        logger.info("DISPATCH issue #%s — %s", issue.number, issue.title)
        self.store.append_event(f"Issue #{issue.number} labeled — dispatching to Devin", issue.number, "dispatch")
        try:
            session_id = self.devin.create_session(PROMPT_TEMPLATE.format(issue_url=issue.html_url))
        except Exception:
            logger.exception("FAILED to create session for issue #%s", issue.number)
            record.update(status="failed", finished_at=utcnow(), summary="session creation failed (see logs)")
            self.store.upsert_result(record)
            self.store.mark_processed(issue.number)
            self.store.append_event(f"Issue #{issue.number}: session creation failed", issue.number, "error")
            return

        record.update(
            session_id=session_id,
            session_url=f"https://app.devin.ai/sessions/{session_id}",
            status="running",
        )
        self.store.upsert_result(record)
        self.store.mark_processed(issue.number)  # never dispatch the same issue twice
        logger.info("RUNNING issue #%s in session %s", issue.number, session_id)
        self.store.append_event(f"Devin session started for issue #{issue.number}", issue.number, "session")

        def on_poll(state) -> None:
            """Surface live progress: detail text, ACU burn, early PR link."""
            prs = state.pull_requests or []
            early_pr = None
            if prs:
                first = prs[0]
                # live v3 shape: {"pr_url": ..., "pr_state": "open"}
                early_pr = (first.get("pr_url") or first.get("url")) if isinstance(first, dict) else str(first)
            narration = state.last_message or state.status_detail
            if narration and narration != record["status_detail"]:
                self.store.append_event(f"Devin (#{issue.number}): {narration}", issue.number, "devin")
            if early_pr and not record["pr_url"]:
                self.store.append_event(f"Pull request opened for issue #{issue.number}: {early_pr}", issue.number, "pr")
            record.update(
                # prefer Devin's chat narration over the coarse working/waiting flag
                status_detail=narration,
                acus_consumed=state.acus_consumed or record["acus_consumed"],
                pr_url=record["pr_url"] or early_pr,
            )
            self.store.upsert_result(record)

        try:
            # done = terminal status, or structured output carrying the PR URL.
            # (Devin initializes structured_output early — its mere presence
            # is NOT completion; issue #4 taught us that.)
            final = self.devin.poll_until_done(
                session_id,
                on_poll=on_poll,
                done_when=lambda s: s.status in TERMINAL_STATUSES
                or bool((s.structured_output or {}).get("pr_url")),
            )
            output = final.structured_output or {}
            # A session that delivered its structured output idles at
            # status="running" — record that as finished. The output must
            # carry the PR URL: Devin fills the schema in incrementally,
            # so partial output is progress, not completion.
            outcome = "finished" if output.get("pr_url") else final.status
            record.update(
                status=outcome,
                status_detail=None,
                pr_url=output.get("pr_url") or record["pr_url"],
                summary=output.get("summary"),
                acus_consumed=final.acus_consumed or record["acus_consumed"],
                finished_at=utcnow(),
            )
            logger.info("%s issue #%s — pr=%s acus=%s", outcome.upper(), issue.number, record["pr_url"], final.acus_consumed)
            self.store.upsert_result(record)
            self.store.append_event(
                f"Issue #{issue.number} {outcome}" + (f" — {record['pr_url']}" if record["pr_url"] else ""),
                issue.number,
                "done" if outcome == "finished" else "error",
            )
            if outcome == "finished":
                self.watch_followups(issue, session_id, record)
        except Exception:
            logger.exception("FAILED while polling session %s (issue #%s)", session_id, issue.number)
            record.update(status="failed", finished_at=utcnow(), summary="polling failed (see logs)")
            self.store.upsert_result(record)
            self.store.append_event(f"Issue #{issue.number}: polling failed", issue.number, "error")

    def watch_followups(self, issue: Issue, session_id: str, record: dict) -> None:
        """A 'finished' session stays alive to address PR review comments.
        Devin's status_detail flips to "working" when it picks one up —
        surface that as status="updating", then refresh the summary when it
        goes idle again. Ends when the session suspends (or after 2 hours).
        """
        deadline = time.monotonic() + 2 * 60 * 60
        interval = int(os.environ.get("FOLLOWUP_POLL_INTERVAL", "60"))
        while time.monotonic() < deadline and not self._stop.is_set():
            self._stop.wait(interval)
            try:
                state = self.devin.get_session(session_id)
            except Exception as exc:
                logger.warning("follow-up poll failed (%s); will retry", type(exc).__name__)
                continue
            if state.status in ("suspended", "stopped", "expired", "finished"):
                logger.info("session %s settled (%s); follow-up watch done for issue #%s", session_id, state.status, issue.number)
                return
            output = state.structured_output or {}
            if state.status_detail == "working":
                if record["status"] != "updating":
                    logger.info("UPDATING issue #%s — session addressing PR feedback", issue.number)
                    self.store.append_event(f"Issue #{issue.number}: Devin addressing PR review feedback", issue.number, "devin")
                record.update(status="updating", status_detail="addressing PR feedback")
            else:
                record.update(
                    status="finished",
                    status_detail=None,
                    summary=output.get("summary") or record["summary"],
                    pr_url=output.get("pr_url") or record["pr_url"],
                    acus_consumed=state.acus_consumed or record["acus_consumed"],
                )
            self.store.upsert_result(record)

    # ---- main loop -----------------------------------------------------------

    def poll_once(self) -> int:
        """One fetch-and-dispatch cycle. Returns how many issues were dispatched."""
        try:
            labeled = self.issues.fetch_labeled_issues()
        except Exception as exc:
            logger.warning("issue fetch failed (%s); retrying next cycle", type(exc).__name__)
            return 0
        fresh = select_new_issues(labeled, self.store.processed)
        for issue in fresh:
            thread = threading.Thread(target=self.dispatch, args=(issue,), name=f"issue-{issue.number}", daemon=True)
            thread.start()
            self._threads.append(thread)
        return len(fresh)

    def run(self, run_once: bool = False) -> None:
        logger.info(
            "orchestrator up [build: pr-gated-completion+retries] — devin=%s issues=%s poll_interval=%ss",
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
        # In RUN_ONCE mode wait for everything; otherwise give threads a
        # short grace period — they are daemons polling sessions that keep
        # running in Devin's cloud regardless of this process.
        deadline = None if run_once else time.monotonic() + 15
        for thread in self._threads:
            thread.join(None if deadline is None else max(0.1, deadline - time.monotonic()))
        stragglers = [t.name for t in self._threads if t.is_alive()]
        if stragglers:
            logger.warning(
                "exiting with %d session-tracker(s) still in flight (%s) — "
                "sessions continue in Devin's cloud", len(stragglers), ", ".join(stragglers),
            )
        logger.info("orchestrator stopped — %d result(s) in %s", len(self.store.results), self.store.results_path)

    def stop(self, *_args) -> None:
        if self._stop.is_set():  # second Ctrl-C = force quit
            logger.warning("force exit")
            os._exit(130)
        logger.info("shutdown requested; finishing up (Ctrl-C again to force quit)…")
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
    # publish the active config so the dashboard reflects reality
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "config.json").write_text(json.dumps({
        "repo": os.environ.get("GITHUB_REPO", "madelinehpark/superset"),
        "label": os.environ.get("ORCHESTRATOR_LABEL", "auto-fix"),
        "max_acu_limit": int(os.environ.get("MAX_ACU_LIMIT", "5")),
        "devin_mode": os.environ.get("DEVIN_MODE", "mock"),
    }, indent=2))
    signal.signal(signal.SIGTERM, orchestrator.stop)
    signal.signal(signal.SIGINT, orchestrator.stop)
    orchestrator.run(run_once=os.environ.get("RUN_ONCE", "") == "1")


if __name__ == "__main__":
    main()
