"""What the live stream reads from ``task_events``: the chunks after an id and the backfill of a new client."""

from __future__ import annotations

import pytest

from app.services import task_events
from app.task_contract import EVENT_LOG, EVENT_PROGRESS, EVENT_REVOKED
from tests.test_task_queue_api import _add_events, _seed

pytestmark = pytest.mark.integration


def test_events_after_an_id_come_oldest_first_in_chunks_and_only_of_that_task(
    db, mock_user, monkeypatch
):
    monkeypatch.setattr(task_events, "BATCH", 2)
    _, task = _seed(db, mock_user)
    ids = _add_events(db, task, *[EVENT_LOG] * 5)
    _, other = _seed(db, mock_user)
    _add_events(db, other, EVENT_LOG, EVENT_LOG)  # events of another task never leak in

    first = task_events.events_after(db, task.taskId, 0)
    second = task_events.events_after(db, task.taskId, first[-1].id)
    third = task_events.events_after(db, task.taskId, second[-1].id)

    assert [e.id for e in first] == ids[:2]
    assert [e.id for e in second] == ids[2:4]
    assert [e.id for e in third] == ids[4:]
    assert task_events.events_after(db, task.taskId, ids[-1]) == []


def test_the_backfill_of_a_new_client_is_the_tail_of_the_transcript_oldest_first(
    db, mock_user, monkeypatch
):
    monkeypatch.setattr(task_events, "BACKFILL_EVENTS", 3)
    _, task = _seed(db, mock_user)
    ids = _add_events(db, task, EVENT_PROGRESS, *[EVENT_LOG] * 5)

    shown = task_events.backfill(db, task.taskId)

    # The last three log lines; the older progress event is added below as the stepper's state.
    assert [e.id for e in shown] == [ids[0], *ids[3:]]


def test_the_stepper_state_is_the_newest_progress_event_even_when_the_tail_has_none(
    db, mock_user, monkeypatch
):
    monkeypatch.setattr(task_events, "BACKFILL_EVENTS", 3)
    _, task = _seed(db, mock_user)
    ids = _add_events(db, task, EVENT_PROGRESS, EVENT_PROGRESS, *[EVENT_LOG] * 4)

    shown = task_events.backfill(db, task.taskId)

    assert [(e.id, e.type) for e in shown] == [
        (ids[1], EVENT_PROGRESS),  # the newer of the two, not the first
        (ids[3], EVENT_LOG),
        (ids[4], EVENT_LOG),
        (ids[5], EVENT_LOG),
    ]


def test_a_progress_event_already_in_the_tail_is_not_sent_twice(db, mock_user, monkeypatch):
    monkeypatch.setattr(task_events, "BACKFILL_EVENTS", 3)
    _, task = _seed(db, mock_user)
    ids = _add_events(db, task, EVENT_LOG, EVENT_LOG, EVENT_PROGRESS, EVENT_LOG)

    assert [e.id for e in task_events.backfill(db, task.taskId)] == ids[1:]


def test_the_backfill_of_a_task_with_no_progress_is_just_its_tail(db, mock_user):
    _, task = _seed(db, mock_user)
    ids = _add_events(db, task, EVENT_LOG, EVENT_LOG)

    assert [e.id for e in task_events.backfill(db, task.taskId)] == ids


def test_the_backfill_of_a_task_without_events_is_empty(db, mock_user):
    _, task = _seed(db, mock_user)

    assert task_events.backfill(db, task.taskId) == []


def test_an_event_written_by_the_api_is_part_of_the_stream_once_committed(db, mock_user):
    _, task = _seed(db, mock_user)
    payload = {"type": EVENT_REVOKED, "status": "cancelled"}

    task_events.add_event(db, task.taskId, EVENT_REVOKED, payload)
    db.commit()

    [event] = task_events.events_after(db, task.taskId, 0)
    assert (event.type, event.payload) == (EVENT_REVOKED, payload)
