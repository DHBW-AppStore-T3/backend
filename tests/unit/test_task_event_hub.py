"""The API's LISTEN connection and the fan-out to the live streams (no database: a fake connection)."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import psycopg
import pytest

from app.services import task_events
from app.services.task_events import EventHub
from app.task_contract import (
    EVENT_FAILED,
    EVENT_LOG,
    EVENT_PROGRESS,
    EVENT_REVOKED,
    EVENT_SUCCEEDED,
    NOTIFY_TASK_EVENTS,
)

pytestmark = pytest.mark.unit


def notify(task_id: str, event_type: str, event_id: int = 7) -> tuple[str, str]:
    return NOTIFY_TASK_EVENTS, f"{task_id}:{event_id}:{event_type}"


# ------------------------------------------------------------------
# Subscriptions and dispatch
# ------------------------------------------------------------------
def test_a_stream_is_woken_only_by_events_of_its_own_task():
    hub = EventHub()
    with hub.subscribe("task-1") as mine, hub.subscribe("task-2") as other:
        hub._dispatch(*notify("task-1", EVENT_LOG))

        assert mine.is_set() and not other.is_set()


def test_every_stream_of_a_task_is_woken_and_leaving_one_keeps_the_others():
    hub = EventHub()
    with hub.subscribe("task-1") as first:
        with hub.subscribe("task-1") as second:
            hub._dispatch(*notify("task-1", EVENT_LOG))
            assert first.is_set() and second.is_set()
        first.clear()

        hub._dispatch(*notify("task-1", EVENT_LOG))
        assert first.is_set()  # the second stream left; the first still listens

    assert hub._subscribers == {}  # nothing is kept for a task nobody follows


def test_notifications_of_other_channels_are_ignored():
    hub = EventHub()
    with hub.subscribe("task-1") as wake:
        hub._dispatch("some_other_channel", "task-1:7:task-log")

        assert not wake.is_set()


@pytest.mark.parametrize("terminal", [EVENT_SUCCEEDED, EVENT_FAILED, EVENT_REVOKED])
def test_a_terminal_event_is_reported_once_with_the_task_it_ends(terminal):
    hub = EventHub()
    ended = []
    hub.on_terminal = ended.append

    hub._dispatch(*notify("task-1", terminal))

    assert ended == ["task-1"]


@pytest.mark.parametrize("ordinary", [EVENT_LOG, EVENT_PROGRESS])
def test_ordinary_events_do_not_end_a_task(ordinary):
    hub = EventHub()
    ended = []
    hub.on_terminal = ended.append

    hub._dispatch(*notify("task-1", ordinary))

    assert ended == []


def test_a_terminal_event_without_a_follow_up_hook_is_harmless():
    hub = EventHub()
    with hub.subscribe("task-1") as wake:
        hub._dispatch(*notify("task-1", EVENT_SUCCEEDED))

        assert wake.is_set()  # the stream still learns about it


def test_a_malformed_notification_wakes_nobody_and_breaks_nothing():
    hub = EventHub()
    ended = []
    hub.on_terminal = ended.append
    with hub.subscribe("task-1") as wake:
        hub._dispatch(NOTIFY_TASK_EVENTS, "garbage")
        hub._dispatch(NOTIFY_TASK_EVENTS, "")

        assert not wake.is_set() and ended == []


# ------------------------------------------------------------------
# The LISTEN loop
# ------------------------------------------------------------------
class FakeConnection:
    """What ``psycopg.AsyncConnection.connect`` returns: LISTENs, then delivers the notifications and ends."""

    def __init__(self, notifications):
        self.statements = []
        self._notifications = notifications

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, statement):
        self.statements.append(statement)

    async def notifies(self):
        for channel, payload in self._notifications:
            yield SimpleNamespace(channel=channel, payload=payload)


def _sleeps_until(monkeypatch, count):
    """Record the back-off sleeps of the loop (without waiting) and cancel it at the ``count``-th."""
    sleeps = []

    async def recording_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == count:
            raise asyncio.CancelledError

    monkeypatch.setattr(task_events.asyncio, "sleep", recording_sleep)
    return sleeps


async def test_the_hub_listens_wakes_the_streams_and_reports_terminal_events(monkeypatch):
    connection = FakeConnection([notify("task-1", EVENT_LOG), notify("task-2", EVENT_SUCCEEDED)])

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    sleeps = _sleeps_until(monkeypatch, 1)
    hub = EventHub()
    seen = []
    hub.on_terminal = lambda task_id: seen.append((task_id, hub.listening))

    with (
        hub.subscribe("task-1") as one,
        hub.subscribe("task-2") as two,
        hub.subscribe("task-3") as three,
    ):
        three.clear()
        with pytest.raises(asyncio.CancelledError):
            await hub._run()

        assert connection.statements == [f"LISTEN {NOTIFY_TASK_EVENTS}"]
        assert seen == [("task-2", True)]  # reported while the hub was listening
        assert one.is_set() and two.is_set()
        # Everybody is woken when listening starts (a stream may have missed events before) and again
        # when the connection ends.
        assert three.is_set() and hub.listening is False
    assert sleeps == [1.0]  # the loop reconnects after a short pause


async def test_a_lost_connection_wakes_the_streams_and_retries_with_a_growing_back_off(
    monkeypatch, caplog
):
    attempts = []

    async def connect(*args, **kwargs):
        attempts.append(1)
        raise OSError("connection refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    sleeps = _sleeps_until(monkeypatch, 7)
    hub = EventHub()

    with (
        hub.subscribe("task-1") as wake,
        caplog.at_level(logging.WARNING, logger="app.services.task_events"),
    ):
        with pytest.raises(asyncio.CancelledError):
            await hub._run()

        assert wake.is_set() and hub.listening is False
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]  # doubles, and stops growing at 30 s
    assert len(attempts) == 7 and "LISTEN connection lost" in caplog.text


async def test_the_back_off_starts_again_after_a_successful_connection(monkeypatch):
    outcomes = [OSError("down"), OSError("still down"), FakeConnection([]), OSError("down again")]

    async def connect(*args, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    sleeps = _sleeps_until(monkeypatch, 4)
    hub = EventHub()

    with pytest.raises(asyncio.CancelledError):
        await hub._run()

    # The connection that worked reset the wait: 1 s after it ended, then 2 s (not the 4 s / 8 s a hub that
    # never forgot its failures would wait).
    assert sleeps == [1.0, 2.0, 1.0, 2.0]


async def test_start_runs_one_listener_and_stop_ends_it(monkeypatch):
    async def never_connects(*args, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", never_connects)
    hub = EventHub()

    hub.start()
    listener = hub._task
    hub.start()  # a second start does not add a second connection
    assert hub._task is listener and not listener.done()
    await asyncio.sleep(0)

    await hub.stop()

    assert listener.cancelled() and hub._task is None and hub.listening is False
    await hub.stop()  # stopping twice is harmless
