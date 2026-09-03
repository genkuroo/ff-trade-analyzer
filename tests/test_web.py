"""Every page and every API endpoint, against a league with no games played.

That is the state a real league spends its whole preseason in, and it is where
empty-data bugs live: a table of zeroes, a division by zero, or a 500 because
something assumed a week had been scored.
"""

from __future__ import annotations

import pytest

import conftest
from conftest import LEAGUE_ID


@pytest.fixture
def client(conn, monkeypatch):
    import app as web
    return web.app.test_client()


@pytest.mark.parametrize(
    "path",
    [
        "/league/{lid}",
        "/league/{lid}/trades",
        "/league/{lid}/machine",
        "/league/{lid}/players",
        "/league/{lid}/team/1",
        "/league/{lid}/player/rb1",
    ],
)
def test_every_page_renders_before_any_game_is_played(client, path):
    assert client.get(path.format(lid=LEAGUE_ID)).status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/power", "/api/trades", "/api/propose?give=Alpha+Runner&get=Bravo+Runner"]
)
def test_api_endpoints_answer(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.get_json() is not None


def test_pages_still_render_once_a_week_has_been_played(client, conn):
    scores = {rid: {p: 5.0 for p in players} for rid, players in conftest.ROSTERS.items()}
    conftest.add_week(
        conn, 1, scores, {rid: set(p) for rid, p in conftest.ROSTERS.items()},
        {1: 100, 2: 90, 3: 80, 4: 70}, {1: 1, 2: 1, 3: 2, 4: 2},
    )
    body = client.get(f"/league/{LEAGUE_ID}").data.decode()
    assert "All-play" in body
    assert client.get(f"/league/{LEAGUE_ID}/player/rb1").status_code == 200


def test_unknown_league_and_player_are_404_not_500(client):
    assert client.get("/league/nope").status_code == 404
    assert client.get(f"/league/{LEAGUE_ID}/player/nope").status_code == 404
    assert client.get(f"/league/{LEAGUE_ID}/team/99").status_code == 404
    assert client.get("/api/power?league=nope").status_code == 404


def test_a_bad_proposal_is_a_400_with_a_readable_message(client):
    response = client.get("/api/propose?give=Nobody&get=Alpha+Runner")
    assert response.status_code == 400
    assert "Nobody" in response.get_json()["error"]


def test_healthz_reports_the_league_count(client):
    payload = client.get("/healthz").get_json()
    assert payload["ok"] is True
    assert payload["leagues"] == 1


def test_the_machine_grades_from_query_parameters_alone(client):
    body = client.get(
        f"/league/{LEAGUE_ID}/machine?a=1&b=2&give=rb1&get=rb2"
    ).data.decode()
    # A graded proposal is meant to be a shareable link, so the URL alone has
    # to be enough to reproduce the grade.
    assert "Alpha Runner" in body and "Bravo Runner" in body
    assert "value" in body.lower()
