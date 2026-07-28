from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from health_exporter.db import Database
from health_exporter.webapp import WebAuth, create_web_app


def _source(
    database: Database,
    vendor: str,
    installation: str,
    priority: int,
) -> tuple[int, str]:
    source_id = database.ensure_source(
        vendor=vendor,
        app_package=f"test.{vendor}.health",
        app_version="1.0",
        installation_hash=installation,
        account_hash="account",
        timezone="Asia/Shanghai",
    )
    database.connection.execute(
        "UPDATE profile_sources SET priority=? WHERE source_id=?",
        (priority, source_id),
    )
    run_id = database.start_run(source_id, "fixture", "1.0", "serial", {})
    return source_id, run_id


def _database(tmp_path: Path) -> tuple[Path, Database]:
    data_dir = tmp_path / "data"
    database = Database(data_dir / "health.sqlite3", data_dir / "private")
    return data_dir, database


def test_web_api_selects_source_priority_without_overwriting_candidates(
    tmp_path: Path,
) -> None:
    data_dir, database = _database(tmp_path)
    oppo_id, oppo_run = _source(database, "oppo", "oppo-install", 10)
    xiaomi_id, xiaomi_run = _source(database, "xiaomi", "xiaomi-install", 20)
    database.upsert_daily_metric(
        source_id=oppo_id,
        canonical_metric="activity",
        local_date="2026-01-02",
        summary={"steps": 8000},
        chart={"series": {}, "intervals": []},
        fidelity="exact_source",
        is_partial=False,
        run_id=oppo_run,
    )
    database.upsert_daily_metric(
        source_id=xiaomi_id,
        canonical_metric="activity",
        local_date="2026-01-02",
        summary={"steps": 7000},
        chart={"series": {}, "intervals": []},
        fidelity="exact_ui",
        is_partial=False,
        run_id=xiaomi_run,
    )
    database.connection.commit()
    database.close()

    with TestClient(create_web_app(tmp_path, data_dir)) as client:
        overview = client.get(
            "/api/v1/overview",
            params={"from": "2026-01-02", "to": "2026-01-02", "source": "unified"},
        )
        detail = client.get(
            "/api/v1/metrics/activity/days/2026-01-02",
            params={"source": "unified"},
        )

    assert overview.status_code == 200
    selected = overview.json()["metrics"]["activity"]["days"][0]
    assert selected["source_id"] == xiaomi_id
    assert selected["summary"]["steps"] == 7000
    assert len(detail.json()["candidates"]) == 2
    assert sum(item["selected"] for item in detail.json()["candidates"]) == 1


def test_web_api_prefers_fidelity_when_priorities_match(tmp_path: Path) -> None:
    data_dir, database = _database(tmp_path)
    source_a, run_a = _source(database, "oppo", "a", 10)
    source_b, run_b = _source(database, "xiaomi", "b", 10)
    for source_id, run_id, fidelity in (
        (source_a, run_a, "exact_source"),
        (source_b, run_b, "visual_only"),
    ):
        database.upsert_daily_metric(
            source_id=source_id,
            canonical_metric="heart_rate",
            local_date="2026-02-03",
            summary={"average_bpm": 66 if source_id == source_a else 80},
            chart={"series": {}, "intervals": []},
            fidelity=fidelity,
            is_partial=False,
            run_id=run_id,
        )
    database.connection.commit()
    database.close()

    with TestClient(create_web_app(tmp_path, data_dir)) as client:
        response = client.get(
            "/api/v1/metrics/heart_rate/daily",
            params={"from": "2026-02-03", "to": "2026-02-03"},
        )

    assert response.status_code == 200
    assert response.json()["days"][0]["source_id"] == source_a


def test_lan_auth_protects_api_and_uses_http_only_cookie(tmp_path: Path) -> None:
    data_dir, database = _database(tmp_path)
    database.close()
    auth = WebAuth(data_dir / "private" / "web-auth.json", enabled=True)
    auth.set_password("correct-horse")

    with TestClient(create_web_app(tmp_path, data_dir, auth=auth)) as client:
        assert client.get("/api/v1/auth/status").status_code == 200
        assert client.get("/api/v1/meta").status_code == 401
        assert (
            client.post(
                "/api/v1/auth/login", json={"password": "wrong-password"}
            ).status_code
            == 401
        )
        login = client.post(
            "/api/v1/auth/login", json={"password": "correct-horse"}
        )
        assert login.status_code == 200
        assert "HttpOnly" in login.headers["set-cookie"]
        assert client.get("/api/v1/meta").status_code == 200


def test_web_api_rejects_invalid_dates_and_unregistered_artifacts(
    tmp_path: Path,
) -> None:
    data_dir, database = _database(tmp_path)
    database.close()
    with TestClient(create_web_app(tmp_path, data_dir)) as client:
        bad_date = client.get(
            "/api/v1/overview",
            params={"from": "2026-99-99", "to": "2026-01-01"},
        )
        missing_artifact = client.get(f"/api/v1/artifacts/{'a' * 64}")

    assert bad_date.status_code == 400
    assert missing_artifact.status_code == 404
