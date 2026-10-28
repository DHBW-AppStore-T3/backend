"""A request body over the limit is refused before it is parsed (app/utils/body_limit.py)."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.utils.body_limit import TOO_LARGE, BodySizeLimit

pytestmark = pytest.mark.unit

LIMIT = 1000


def _deny():
    raise HTTPException(status_code=401, detail="not logged in")


def _client(limit: int = LIMIT) -> TestClient:
    app = FastAPI()
    app.add_middleware(BodySizeLimit, max_bytes=limit)

    @app.post("/echo")
    def echo(payload: dict):
        return {"keys": sorted(payload)}

    # The way the API is built: the login is a dependency, which FastAPI runs after it
    # has read the body.
    @app.post("/protected")
    def protected(payload: dict, _user=Depends(_deny)):
        return payload

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(app)


def _json(size: int) -> bytes:
    filler = "x" * (size - len('{"a":""}'))
    return f'{{"a":"{filler}"}}'.encode()


def test_a_body_within_the_limit_passes():
    response = _client().post(
        "/echo", content=_json(LIMIT), headers={"content-type": "application/json"}
    )
    assert response.status_code == 200 and response.json() == {"keys": ["a"]}


def test_a_declared_length_over_the_limit_is_refused_without_reading_the_body():
    response = _client().post(
        "/echo", content=_json(LIMIT + 1), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == TOO_LARGE
    assert response.json()["detail"]["limit_bytes"] == LIMIT


def test_a_chunked_body_is_counted_as_it_arrives():
    def chunks():
        for _ in range(20):
            yield b"x" * 100  # 2000 bytes, no Content-Length announced

    response = _client().post(
        "/echo", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == TOO_LARGE


def test_a_chunked_body_within_the_limit_passes():
    def chunks():
        yield b'{"a":'
        yield b'"b"}'

    response = _client().post(
        "/echo", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 200


def test_the_limit_applies_before_the_login_is_looked_at():
    big = _client().post(
        "/protected", content=_json(LIMIT + 1), headers={"content-type": "application/json"}
    )
    assert big.status_code == 413
    small = _client().post(
        "/protected", content=_json(100), headers={"content-type": "application/json"}
    )
    assert small.status_code == 401


def test_requests_without_a_body_are_not_affected():
    assert _client().get("/ping").status_code == 200


def test_a_limit_of_zero_switches_it_off():
    response = _client(0).post(
        "/echo", content=_json(5000), headers={"content-type": "application/json"}
    )
    assert response.status_code == 200
