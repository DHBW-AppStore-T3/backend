"""The age of the virus signature database is checked before each scan (app/services/malware_scan.py)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import settings
from app.services import malware_scan
from tests.unit.test_upload_formats import clamd, version_reply  # noqa: F401  (the fake daemon)

pytestmark = pytest.mark.unit


def test_a_fresh_database_scans_normally_and_without_a_warning(clamd, caplog):  # noqa: F811
    with caplog.at_level("WARNING", logger="app.services.malware_scan"):
        malware_scan.scan(b"x")
    assert clamd.received == [b"x"] and not caplog.records


def test_an_old_database_is_logged_but_still_scans(clamd, caplog):  # noqa: F811
    clamd.version = version_reply(4)
    with caplog.at_level("WARNING", logger="app.services.malware_scan"):
        malware_scan.scan(b"x")
    assert clamd.received == [b"x"]
    assert any(r.getMessage().startswith("clamd_database_old") for r in caplog.records)


def test_a_database_past_the_maximum_age_fails_the_scan_closed(clamd, caplog):  # noqa: F811
    clamd.version = version_reply(9)
    with (
        caplog.at_level("ERROR", logger="app.services.malware_scan"),
        pytest.raises(malware_scan.ScanFailed) as exc,
    ):
        malware_scan.scan(b"x")
    assert "9 days old" in str(exc.value)
    assert clamd.received == []  # the file was not even sent
    assert any(r.getMessage().startswith("clamd_database_stale") for r in caplog.records)


def test_a_version_answer_without_a_date_does_not_block_uploads(clamd, caplog):  # noqa: F811
    clamd.version = b"ClamAV 1.4.3\0"
    with caplog.at_level("WARNING", logger="app.services.malware_scan"):
        malware_scan.scan(b"x")
    assert clamd.received == [b"x"]
    assert any(r.getMessage().startswith("clamd_database_age_unknown") for r in caplog.records)


def test_the_age_check_can_be_switched_off(clamd):  # noqa: F811
    clamd.version = version_reply(400)
    old = (settings.CLAMD_WARN_DB_AGE_DAYS, settings.CLAMD_MAX_DB_AGE_DAYS)
    settings.CLAMD_WARN_DB_AGE_DAYS = settings.CLAMD_MAX_DB_AGE_DAYS = 0
    try:
        malware_scan.scan(b"x")
    finally:
        settings.CLAMD_WARN_DB_AGE_DAYS, settings.CLAMD_MAX_DB_AGE_DAYS = old
    assert clamd.received == [b"x"]


def test_the_age_is_asked_once_in_a_while_not_for_every_file(clamd):  # noqa: F811
    clamd.version = version_reply(9)
    with pytest.raises(malware_scan.ScanFailed):
        malware_scan.scan(b"x")
    clamd.version = version_reply(0)  # updated since, but the earlier answer is still cached
    with pytest.raises(malware_scan.ScanFailed):
        malware_scan.scan(b"x")
    malware_scan._db_age_cache.clear()
    malware_scan.scan(b"x")
    assert clamd.received == [b"x"]


def test_parse_db_age_days_reads_the_official_answer():
    now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    assert malware_scan.parse_db_age_days(
        "ClamAV 1.4.3/28166/Tue Oct 27 12:00:00 2026", now
    ) == pytest.approx(1)
    assert malware_scan.parse_db_age_days(
        "ClamAV 1.4.3/28166/Thu Oct  1 12:00:00 2026", now
    ) == pytest.approx(27)
    assert malware_scan.parse_db_age_days("ClamAV 1.4.3", now) is None
    assert malware_scan.parse_db_age_days("ClamAV 1.4.3/1/not a date", now) is None
