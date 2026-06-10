"""Dedupe: an issue must never be dispatched twice, across restarts too."""

from github_client import Issue
from orchestrator import StateStore, select_new_issues

ISSUES = [
    Issue(1, "first", "https://example.com/1"),
    Issue(2, "second", "https://example.com/2"),
    Issue(3, "third", "https://example.com/3"),
]


def test_select_new_issues_filters_processed():
    assert select_new_issues(ISSUES, processed=set()) == ISSUES
    assert select_new_issues(ISSUES, processed={1, 3}) == [ISSUES[1]]
    assert select_new_issues(ISSUES, processed={1, 2, 3}) == []


def test_processed_set_persists_across_store_instances(tmp_path):
    processed = tmp_path / "processed.json"
    results = tmp_path / "results.json"

    store = StateStore(processed_path=processed, results_path=results)
    store.mark_processed(1)
    store.mark_processed(3)

    reloaded = StateStore(processed_path=processed, results_path=results)
    assert reloaded.processed == {1, 3}
    assert select_new_issues(ISSUES, reloaded.processed) == [ISSUES[1]]


def test_upsert_result_updates_in_place_not_duplicates(tmp_path):
    store = StateStore(
        processed_path=tmp_path / "processed.json",
        results_path=tmp_path / "results.json",
    )
    store.upsert_result({"issue_number": 7, "status": "running"})
    store.upsert_result({"issue_number": 7, "status": "finished"})
    assert len(store.results) == 1
    assert store.results[0]["status"] == "finished"
