"""Devin API client — one interface, real and mock implementations.

Selection happens via the DEVIN_MODE env var ("mock" | "real", default mock).
"""

from __future__ import annotations

import abc
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# Observed via the live v3 API (state/smoke_raw.json): a session that has
# completed its task keeps status="running" while it awaits further
# instructions, then drifts to "suspended". It may never report "finished".
# The reliable completion signal is a populated structured_output.
TERMINAL_STATUSES = {"blocked", "finished", "suspended", "expired", "stopped"}

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "pr_url": {"type": "string"},
        "status": {"type": "string"},
        "summary": {"type": "string"},
    },
}


@dataclass
class SessionState:
    session_id: str
    status: str
    structured_output: Optional[dict] = None
    acus_consumed: Optional[float] = None
    status_detail: Optional[str] = None
    pull_requests: Optional[list] = None
    last_message: Optional[str] = None  # most recent agent chat message

    @property
    def is_done(self) -> bool:
        """Done = terminal status OR the agent delivered its structured output
        (sessions idle at status="running" after completing the task)."""
        return self.status in TERMINAL_STATUSES or bool(self.structured_output)


class DevinClient(abc.ABC):
    """Create a session, check on it, poll it to a terminal state."""

    @abc.abstractmethod
    def create_session(self, prompt: str) -> str:
        """Start a session; returns its session id."""

    @abc.abstractmethod
    def get_session(self, session_id: str) -> SessionState:
        """Fetch current status + structured output for a session."""

    def poll_until_done(
        self,
        session_id: str,
        timeout_seconds: int = 60 * 60,
        backoff_initial: float = 5.0,
        backoff_cap: float = 30.0,
        on_poll=None,
        done_when=None,
    ) -> SessionState:
        """Poll with exponential backoff (capped) until blocked/finished.

        Sessions routinely run 15–45 minutes, so the default overall
        timeout is generous. Raises TimeoutError if exceeded.
        `on_poll(state)` is invoked after every poll, so callers can
        surface live progress (status_detail, ACUs, early PR links).
        `done_when(state)` overrides the completion test — needed because
        Devin may initialize structured_output EARLY and update it
        incrementally, so "any output present" can fire prematurely.
        """
        deadline = time.monotonic() + timeout_seconds
        delay = backoff_initial
        is_done = done_when or (lambda s: s.is_done)
        consecutive_errors = 0
        while True:
            try:
                state = self.get_session(session_id)
                consecutive_errors = 0
            except Exception as exc:
                # transient network blips must not kill a 30-minute session
                consecutive_errors += 1
                logger.warning(
                    "poll error %d/5 for %s (%s); retrying",
                    consecutive_errors, session_id, type(exc).__name__,
                )
                if consecutive_errors >= 5:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, backoff_cap)
                continue
            if on_poll:
                on_poll(state)
            if is_done(state):
                logger.info(
                    "session %s done (status=%s, structured_output=%s)",
                    session_id, state.status, "yes" if state.structured_output else "no",
                )
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError(f"session {session_id} not terminal after {timeout_seconds}s")
            logger.debug("session %s still %s; next poll in %.0fs", session_id, state.status, delay)
            time.sleep(delay)
            delay = min(delay * 2, backoff_cap)


class RealDevinClient(DevinClient):
    """Devin REST API v3 (org-scoped service-user key).

    v1 (personal-key) fallback is wired but requires a personal API key —
    a v3 `cog_...` service-user key gets 403 from v1 endpoints.
    """

    V3_BASE = "https://api.devin.ai/v3/organizations/{org_id}/sessions"
    V1_CREATE = "https://api.devin.ai/v1/sessions"
    V1_GET = "https://api.devin.ai/v1/session/{session_id}"

    def __init__(
        self,
        api_key: str,
        org_id: str,
        max_acu_limit: int = 5,
        api_version: str = "v3",
        request_timeout: float = 30.0,
        idempotent: bool = True,
    ) -> None:
        if not api_key:
            raise ValueError("DEVIN_API_KEY is required in real mode")
        if api_version == "v3" and not org_id:
            raise ValueError("DEVIN_ORG_ID is required for the v3 API")
        self.api_key = api_key
        self.org_id = org_id
        self.max_acu_limit = max_acu_limit
        self.api_version = api_version
        self.request_timeout = request_timeout
        self.idempotent = idempotent
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"
        retry = requests.adapters.Retry(
            total=2, backoff_factor=1.0,
            status_forcelist=[429, 502, 503, 504],
            allowed_methods=["GET"],  # POSTs surface errors to the caller
        )
        self._session.mount("https://", requests.adapters.HTTPAdapter(max_retries=retry))

    def _create_url(self) -> str:
        if self.api_version == "v1":
            return self.V1_CREATE
        return self.V3_BASE.format(org_id=self.org_id)

    def _get_url(self, session_id: str) -> str:
        if self.api_version == "v1":
            return self.V1_GET.format(session_id=session_id)
        return self.V3_BASE.format(org_id=self.org_id) + f"/{session_id}"

    def create_session(self, prompt: str) -> str:
        body = {
            "prompt": prompt,
            "max_acu_limit": self.max_acu_limit,
            # idempotent=True dedupes identical prompts to the same session;
            # set IDEMPOTENT=false to force a fresh session for a re-run
            "idempotent": self.idempotent,
            "tags": ["auto-remediation"],
            "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
        }
        resp = self._session.post(self._create_url(), json=body, timeout=self.request_timeout)
        resp.raise_for_status()
        data = resp.json()
        session_id = data.get("id") or data.get("session_id")
        if not session_id:
            raise ValueError(f"no session id in create response: {list(data)}")
        logger.info("created Devin session %s", session_id)
        return session_id

    def get_session(self, session_id: str) -> SessionState:
        resp = self._session.get(self._get_url(session_id), timeout=self.request_timeout)
        resp.raise_for_status()
        data = resp.json()
        # live v3 returns status_enum=null; the populated field is "status"
        status = data.get("status_enum") or data.get("status") or "unknown"
        return SessionState(
            session_id=session_id,
            status=str(status).lower(),
            structured_output=data.get("structured_output"),
            acus_consumed=data.get("acus_consumed"),
            status_detail=data.get("status_detail"),
            pull_requests=data.get("pull_requests"),
            last_message=self._latest_agent_message(session_id),
        )

    def _latest_agent_message(self, session_id: str) -> Optional[str]:
        """The session's raw status_detail is just working/waiting_for_user;
        Devin's chat messages carry the human-readable progress narration."""
        if self.api_version != "v3":
            return None
        try:
            resp = self._session.get(
                self._get_url(session_id) + "/messages", timeout=self.request_timeout
            )
            resp.raise_for_status()
            agent_msgs = [m for m in resp.json().get("items", []) if m.get("source") == "devin"]
            if not agent_msgs:
                return None
            text = " ".join((agent_msgs[-1].get("message") or "").split())
            return (text[:120] + "…") if len(text) > 120 else (text or None)
        except Exception:  # progress narration is best-effort only
            return None


class MockDevinClient(DevinClient):
    """No-network stand-in, synced to the live v3 API's observed behavior:
    in-progress sessions report status="running"; a completed session KEEPS
    status="running" and signals completion by populating structured_output
    (see state/smoke_raw.json from the step-27 smoke test).

    Sessions cycle through scenarios by creation order:
      1st: delivers structured output after 2 "running" polls (happy path)
      2nd: slow — delivers after 5 polls
      3rd: ends status="blocked" (failure path)
    then the cycle repeats. Mirrors the demo fleet: green / medium / risky.
    """

    SCENARIOS = ("normal", "slow", "blocked")
    POLLS_BEFORE_TERMINAL = {"normal": 2, "slow": 5, "blocked": 2}

    def __init__(self) -> None:
        self._created = 0
        self._scenario: dict[str, str] = {}
        self._polls: dict[str, int] = {}
        self._index: dict[str, int] = {}

    def create_session(self, prompt: str) -> str:
        scenario = self.SCENARIOS[self._created % len(self.SCENARIOS)]
        self._created += 1
        session_id = f"mock-session-{self._created:03d}"
        self._scenario[session_id] = scenario
        self._polls[session_id] = 0
        self._index[session_id] = self._created
        logger.info("created %s (scenario=%s)", session_id, scenario)
        return session_id

    def get_session(self, session_id: str) -> SessionState:
        if session_id not in self._scenario:
            raise KeyError(f"unknown mock session: {session_id}")
        scenario = self._scenario[session_id]
        self._polls[session_id] += 1
        poll = self._polls[session_id]
        details = ["Reading the issue", "Implementing fix", "Running tests", "Running tests", "Opening pull request"]
        if poll <= self.POLLS_BEFORE_TERMINAL[scenario]:
            return SessionState(
                session_id=session_id,
                status="running",
                status_detail="working",  # raw API value; narration lives in last_message
                last_message=details[min(poll - 1, len(details) - 1)],
                acus_consumed=round(0.3 * poll, 1),
            )
        if scenario == "blocked":
            return SessionState(
                session_id=session_id,
                status="blocked",
                structured_output=None,
                status_detail="Waiting for user input",
                acus_consumed=1.5,
            )
        # like the real API: status stays "running", structured_output appears,
        # status_detail flips to waiting_for_user, then the session suspends
        pr_url = f"https://github.com/example/repo/pull/{900 + self._index[session_id]}"
        done_polls = poll - self.POLLS_BEFORE_TERMINAL[scenario]
        return SessionState(
            session_id=session_id,
            status="suspended" if done_polls > 2 else "running",
            structured_output={
                "pr_url": pr_url,
                "status": "success",
                "summary": f"Mock: fixed the issue and opened a PR ({scenario} path).",
            },
            acus_consumed=2.0,
            status_detail="waiting_for_user",
            pull_requests=[{"pr_url": pr_url, "pr_state": "open"}],  # live v3 shape
        )


def client_from_env() -> DevinClient:
    mode = os.environ.get("DEVIN_MODE", "mock").lower()
    if mode == "real":
        return RealDevinClient(
            api_key=os.environ.get("DEVIN_API_KEY", ""),
            org_id=os.environ.get("DEVIN_ORG_ID", ""),
            max_acu_limit=int(os.environ.get("MAX_ACU_LIMIT", "5")),
            api_version=os.environ.get("DEVIN_API_VERSION", "v3"),
            idempotent=os.environ.get("IDEMPOTENT", "true").lower() != "false",
        )
    return MockDevinClient()
