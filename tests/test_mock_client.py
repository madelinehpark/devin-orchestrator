"""Mock Devin client lifecycle, synced to observed live-API behavior:
completed sessions keep status="running" and signal via structured_output."""

from devin_client import MockDevinClient


def test_normal_session_delivers_output_after_two_running_polls():
    client = MockDevinClient()
    sid = client.create_session("fix issue 1")
    assert client.get_session(sid).status == "running"
    assert client.get_session(sid).status == "running"
    final = client.get_session(sid)
    assert final.status == "running"  # real sessions idle at running when done
    assert final.is_done               # ...but structured_output marks completion
    assert final.structured_output["pr_url"].startswith("https://")


def test_second_session_is_slow_five_polls_before_done():
    client = MockDevinClient()
    client.create_session("first")
    sid = client.create_session("second — slow path")
    for _ in range(5):
        state = client.get_session(sid)
        assert state.status == "running"
        assert not state.is_done
    assert client.get_session(sid).is_done


def test_third_session_ends_blocked_without_output():
    client = MockDevinClient()
    client.create_session("first")
    client.create_session("second")
    sid = client.create_session("third — blocked path")
    final = client.poll_until_done(sid, backoff_initial=0, backoff_cap=0)
    assert final.status == "blocked"
    assert final.is_done
    assert final.structured_output is None


def test_poll_until_done_returns_completed_state():
    client = MockDevinClient()
    sid = client.create_session("happy path")
    final = client.poll_until_done(sid, backoff_initial=0, backoff_cap=0)
    assert final.is_done
    assert "pull/" in final.structured_output["pr_url"]
    assert final.acus_consumed is not None
