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
    ) -> SessionState:
        """Poll with exponential backoff (capped) until blocked/finished.

        Sessions routinely run 15–45 minutes, so the default overall
        timeout is generous. Raises TimeoutError if exceeded.
        """
        deadline = time.monotonic() + timeout_seconds
        delay = backoff_initial
        while True:
            state = self.get_session(session_id)
            if state.is_done:
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
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"

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
            "idempotent": True,
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
        )


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
        if self._polls[session_id] <= self.POLLS_BEFORE_TERMINAL[scenario]:
            return SessionState(session_id=session_id, status="running")
        if scenario == "blocked":
            return SessionState(
                session_id=session_id,
                status="blocked",
                structured_output=None,
                acus_consumed=1.5,
            )
        # like the real API: status stays "running", structured_output appears
        return SessionState(
            session_id=session_id,
            status="running",
            structured_output={
                "pr_url": f"https://github.com/example/repo/pull/{900 + self._index[session_id]}",
                "status": "success",
                "summary": f"Mock: fixed the issue and opened a PR ({scenario} path).",
            },
            acus_consumed=2.0,
        )


def client_from_env() -> DevinClient:
    mode = os.environ.get("DEVIN_MODE", "mock").lower()
    if mode == "real":
        return RealDevinClient(
            api_key=os.environ.get("DEVIN_API_KEY", ""),
            org_id=os.environ.get("DEVIN_ORG_ID", ""),
            max_acu_limit=int(os.environ.get("MAX_ACU_LIMIT", "5")),
            api_version=os.environ.get("DEVIN_API_VERSION", "v3"),
        )
    return MockDevinClient()
