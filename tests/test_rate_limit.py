"""Per-person budgets of the expensive routes (app/utils/rate_limit.py, review S17)."""

from unittest.mock import patch

import pytest

from app.utils import rate_limit
from app.utils.rate_limit import SlidingWindow


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.unit
def test_the_window_counts_per_key_and_slides():
    clock = Clock()
    window = SlidingWindow(clock)

    assert [window.hit("a", 2, 60) for _ in range(2)] == [0.0, 0.0]
    assert window.hit("a", 2, 60) == 60.0
    assert window.hit("b", 2, 60) == 0.0  # another person, another budget

    clock.now += 30
    assert window.hit("a", 2, 60) == 30.0
    clock.now += 31
    assert window.hit("a", 2, 60) == 0.0


@pytest.mark.integration
def test_the_user_search_answers_429_above_its_budget(client):
    with (
        patch.object(rate_limit.limiter, "hit", side_effect=[0.0, 0.0, 12.3]),
        patch("app.routers.users.search_keycloak_users", return_value=[]),
    ):
        assert client.get("/users/search", params={"query": "ab"}).status_code == 200
        assert client.get("/users/search", params={"query": "ab"}).status_code == 200
        limited = client.get("/users/search", params={"query": "ab"})

    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "rate_limited"
    assert limited.headers["Retry-After"] == "13"


@pytest.mark.integration
def test_the_user_search_size_is_bounded(client):
    with patch("app.routers.users.search_keycloak_users", return_value=[]) as search:
        assert client.get("/users/search", params={"query": "ab", "limit": 5000}).status_code == 422
        assert client.get("/users/search", params={"query": "ab", "limit": 50}).status_code == 200
    search.assert_called_once_with("ab", 50)


@pytest.mark.integration
def test_deploying_has_a_budget_per_person(client, mock_user):
    rate_limit.limiter.reset()
    for _ in range(30):
        rate_limit.limiter.hit(f"deploy:{mock_user.userId}", 30)

    response = client.post(
        "/deployments/",
        json={
            "name": "d",
            "appId": "00000000-0000-0000-0000-000000000000",
            "releaseTag": "v1",
            "teams": [],
        },
    )

    assert response.status_code == 429


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "path", "name"),
    [
        ("post", "/apps/", "register-app"),
        ("post", "/apps/00000000-0000-0000-0000-000000000000/versions/v1/submit", "submit-version"),
    ],
)
def test_registering_and_submitting_have_budgets(client, mock_user, method, path, name):
    for _ in range(200):
        rate_limit.limiter.hit(f"{name}:{mock_user.userId}", 200)

    response = getattr(client, method)(path, json={"name": "x", "notes": ""})

    assert response.status_code == 429
