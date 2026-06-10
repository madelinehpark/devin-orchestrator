"""Mock Devin client lifecycle: scenarios, polling, terminal states."""

from devin_client import MockDevinClient


def test_normal_session_finishes_after_two_working_polls():
    client = MockDevinClient()
    sid = client.create_session("fix issue 1")
    assert client.get_session(sid).status == "working"
    assert client.get_session(sid).status == "working"
    final = client.get_session(sid)
    assert final.status == "finished"
    assert final.is_terminal
    assert final.structured_output["pr_url"].startswith("https://")


def test_second_session_is_slow_five_polls_before_finished():
    client = MockDevinClient()
    client.create_session("first")
    sid = client.create_session("second — slow path")
    statuses = [client.get_session(sid).status for _ in range(5)]
    assert statuses == ["working"] * 5
    assert client.get_session(sid).status == "finished"


def test_third_session_ends_blocked():
    client = MockDevinClient()
    client.create_session("first")
    client.create_session("second")
    sid = client.create_session("third — blocked path")
    final = client.poll_until_done(sid, backoff_initial=0, backoff_cap=0)
    assert final.status == "blocked"
    assert final.is_terminal
    assert final.structured_output["pr_url"] == ""


def test_poll_until_done_returns_terminal_state():
    client = MockDevinClient()
    sid = client.create_session("happy path")
    final = client.poll_until_done(sid, backoff_initial=0, backoff_cap=0)
    assert final.status == "finished"
    assert "pull/" in final.structured_output["pr_url"]
