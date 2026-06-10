"""GitHub issue source — real REST API client plus a no-credential mock."""

from __future__ import annotations

import abc
import logging
import os
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass
class Issue:
    number: int
    title: str
    html_url: str


class IssueSource(abc.ABC):
    @abc.abstractmethod
    def fetch_labeled_issues(self) -> list[Issue]:
        """Return open issues carrying the configured label."""


class GitHubClient(IssueSource):
    """Lists open issues with the given label via the GitHub REST API."""

    API = "https://api.github.com"

    def __init__(self, token: str, repo: str, label: str, request_timeout: float = 30.0) -> None:
        if not token:
            raise ValueError("GITHUB_TOKEN is required for the real GitHub client")
        if not repo or "/" not in repo:
            raise ValueError("GITHUB_REPO must look like owner/name")
        self.repo = repo
        self.label = label
        self.request_timeout = request_timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            }
        )

    def fetch_labeled_issues(self) -> list[Issue]:
        issues: list[Issue] = []
        url = f"{self.API}/repos/{self.repo}/issues"
        params = {"state": "open", "labels": self.label, "per_page": 100}
        while url:
            resp = self._session.get(url, params=params, timeout=self.request_timeout)
            resp.raise_for_status()
            for item in resp.json():
                if "pull_request" in item:
                    continue  # the issues endpoint also returns PRs
                issues.append(
                    Issue(
                        number=item["number"],
                        title=item["title"],
                        html_url=item["html_url"],
                    )
                )
            url = resp.links.get("next", {}).get("url")
            params = None  # the "next" link already carries the query string
        logger.debug("found %d open '%s' issue(s) in %s", len(issues), self.label, self.repo)
        return issues


class MockIssueSource(IssueSource):
    """Three canned issues, mirroring the demo fleet. Served once."""

    CANNED = [
        Issue(40405, 'Duplicate button enabled with empty name', "https://github.com/example/repo/issues/40405"),
        Issue(40501, "Relative time comparison offset regression", "https://github.com/example/repo/issues/40501"),
        Issue(40850, "expose_in_sqllab ignored in SQL Lab selector", "https://github.com/example/repo/issues/40850"),
    ]

    def __init__(self) -> None:
        self._served = False

    def fetch_labeled_issues(self) -> list[Issue]:
        if self._served:
            return []
        self._served = True
        return list(self.CANNED)


def issue_source_from_env() -> IssueSource:
    """Real client when a token is configured; canned issues otherwise."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return GitHubClient(
            token=token,
            repo=os.environ.get("GITHUB_REPO", ""),
            label=os.environ.get("ORCHESTRATOR_LABEL", "auto-fix"),
        )
    logger.info("no GITHUB_TOKEN set — using canned mock issues")
    return MockIssueSource()
