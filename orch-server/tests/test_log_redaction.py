"""Dashboard query tokens are omitted from Uvicorn log records."""

import logging

from haniel_orch.server import create_app


def _build_app(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN", "test-node-token")
    monkeypatch.setenv("AUTH_BEARER_TOKEN", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "orchestrator.db"))
    monkeypatch.setenv("DASHBOARD_DIR", str(tmp_path / "missing-dashboard"))
    return create_app()


def test_uvicorn_websocket_log_redacts_query_token(monkeypatch, tmp_path, caplog):
    _build_app(monkeypatch, tmp_path)
    caplog.clear()

    logger = logging.getLogger("uvicorn.error")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        logger.info(
            '%s - "WebSocket %s" [accepted]',
            "1.2.3.4:0",
            "/ws/dashboard?token=TEST_SECRET_VALUE",
        )

    record = next(record for record in caplog.records if record.name == "uvicorn.error")
    assert record.getMessage() == (
        '1.2.3.4:0 - "WebSocket /ws/dashboard?token=***" [accepted]'
    )
    assert "TEST_SECRET_VALUE" not in record.getMessage()


def test_uvicorn_access_log_redacts_query_token(monkeypatch, tmp_path, caplog):
    _build_app(monkeypatch, tmp_path)
    caplog.clear()

    logger = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:0",
            "GET",
            "/dashboard?token=TEST_SECRET_VALUE",
            "1.1",
            200,
        )

    record = next(record for record in caplog.records if record.name == "uvicorn.access")
    assert record.getMessage() == (
        '1.2.3.4:0 - "GET /dashboard?token=*** HTTP/1.1" 200'
    )
    assert "TEST_SECRET_VALUE" not in record.getMessage()


def test_redaction_preserves_other_query_parameters_and_tokenless_paths(
    monkeypatch, tmp_path, caplog
):
    _build_app(monkeypatch, tmp_path)
    caplog.clear()

    logger = logging.getLogger("uvicorn.access")
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logger.info('%s', "/api/history?node=a&token=abc&limit=5")
        logger.info('%s', "/api/history?node=a&limit=5")

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "uvicorn.access"
    ]
    assert messages == [
        "/api/history?node=a&token=***&limit=5",
        "/api/history?node=a&limit=5",
    ]


def test_create_app_installs_one_filter_per_uvicorn_logger(
    monkeypatch, tmp_path
):
    _build_app(monkeypatch, tmp_path)
    _build_app(monkeypatch, tmp_path)

    for logger_name in ("uvicorn.access", "uvicorn.error"):
        filters = logging.getLogger(logger_name).filters
        assert sum(
            filter_.__class__.__name__ == "QueryTokenRedactionFilter"
            and filter_.__class__.__module__ == "haniel_orch.log_redaction"
            for filter_ in filters
        ) == 1
