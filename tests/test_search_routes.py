"""Search failover contracts use explicit local transports; no external search is faked as live."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from soulbot.app import build_services, create_web_app
from soulbot.providers.llm.base import ProviderError
from soulbot.services.backup_service import BackupService, InstanceLock
from soulbot.services.config_service import SearchSettings, Settings
from soulbot.services.search_service import SearchRequest
from soulbot.storage.database import Database


@pytest.fixture
def s(tmp_path):
    services = build_services(tmp_path)
    services.config.current.search = SearchSettings.model_validate(
        {
            "enabled": True,
            "base_url": "https://primary.example",
            "price_cny_per_request": 0.1,
            "fallbacks": {
                "fallback_1": {
                    "enabled": True,
                    "base_url": "https://backup-one.example",
                    "price_cny_per_request": None,
                },
                "fallback_2": {
                    "enabled": True,
                    "base_url": "https://backup-two.example",
                    "price_cny_per_request": 0.3,
                },
            },
        }
    )
    services.calls = []
    return services


def valid():
    return httpx.Response(
        200,
        json={
            "results": [
                {
                    "title": "公开资料",
                    "url": "https://example.org/article",
                    "content": "可核对的公开摘要",
                }
            ]
        },
    )


def transport(s, handler):
    async def dispatch(request):
        s.calls.append(request.url.host)
        assert request.url.path == "/search" and request.url.params["format"] == "json"
        result = handler(request)
        return await result if hasattr(result, "__await__") else result

    s.search.transport = httpx.MockTransport(dispatch)


def run(s, **kwargs):
    return asyncio.run(s.search.search(SearchRequest(query="公开主题"), **kwargs))


def rows(s):
    with s.db.connect() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT u.*,a.request_id,a.provider,a.error_kind FROM search_usage u LEFT JOIN search_attempts a ON a.usage_id=u.id ORDER BY u.rowid"
            )
        ]


@pytest.mark.parametrize(
    "payload",
    [
        {"fallbacks": {"other": {"enabled": True}}},
        {"fallbacks": {"fallback_1": {"enabled": True, "base_url": "http://127.0.0.1:8080/"}}},
        {
            "base_url": "https://PRIMARY.example:443/path/",
            "fallbacks": {
                "fallback_1": {"enabled": True, "base_url": "https://primary.example/path"}
            },
        },
        {"total_timeout_seconds": 91},
        {"fallbacks": {"fallback_1": {"base_url": "https://user:secret@example.org"}}},
    ],
)
def test_schema_rejects_duplicate_enabled_addresses_invalid_keys_and_secret_urls(payload):
    with pytest.raises(ValidationError):
        SearchSettings.model_validate(payload)


def test_primary_success_and_empty_results_never_fall_back(s):
    transport(s, lambda _: httpx.Response(200, json={"results": []}))
    result = run(s)
    assert result["provider_id"] == "primary" and result["items"] == []
    assert s.calls == ["primary.example"]
    assert rows(s)[0]["status"] == "completed"
    assert s.search.provider_status()["items"][0]["state"] == "available"


def test_three_ordered_attempts_keep_distinct_costs_and_unknown_price(s):
    def handler(request):
        return valid() if request.url.host == "backup-two.example" else httpx.Response(503)

    transport(s, handler)
    result = run(s)
    assert result["provider_id"] == "fallback_2"
    assert s.calls == ["primary.example", "backup-one.example", "backup-two.example"]
    data = rows(s)
    assert [r["status"] for r in data] == ["uncertain", "uncertain", "completed"]
    assert [r["cost_cny"] for r in data] == [0.1, None, 0.3]
    assert len({r["request_id"] for r in data}) == 1
    assert [r["provider"] for r in data] == [a["provider"] for a in result["attempts"]]
    summary = s.budget.summary(s.config.current)["search"]["recent"]
    assert summary[0]["provider"] == "fallback_2" and summary[0]["request_id"]
    assert "公开主题" not in json.dumps(s.search.provider_status(), ensure_ascii=False)


@pytest.mark.parametrize("first", [401, 403, 429, 500, 503, "timeout", "network", "invalid"])
def test_service_specific_errors_can_switch_to_an_independent_provider(s, first):
    def handler(request):
        if request.url.host != "primary.example":
            return valid()
        if first == "timeout":
            raise httpx.ReadTimeout("private upstream detail")
        if first == "network":
            raise httpx.ConnectError("private upstream detail")
        if first == "invalid":
            return httpx.Response(200, text="not-json-private")
        return httpx.Response(first, text="private upstream detail")

    transport(s, handler)
    assert run(s)["provider_id"] == "fallback_1"
    assert len(rows(s)) == 2 and rows(s)[0]["status"] == "uncertain"


@pytest.mark.parametrize("status", [400, 422])
def test_invalid_query_errors_do_not_switch(s, status):
    transport(s, lambda _: httpx.Response(status))
    with pytest.raises(ProviderError) as error:
        run(s)
    assert error.value.kind == "input" and len(s.calls) == 1


def test_disabled_backup_is_skipped(s):
    s.config.current.search.fallbacks["fallback_1"].enabled = False
    transport(s, lambda r: valid() if r.url.host == "backup-two.example" else httpx.Response(503))
    assert run(s)["provider_id"] == "fallback_2"
    assert s.calls == ["primary.example", "backup-two.example"]


def test_total_route_timeout_cancels_current_attempt_and_never_reserves_next(s):
    s.config.current.search.total_timeout_seconds = 0.02

    async def handler(_):
        await asyncio.sleep(1)
        return valid()

    transport(s, handler)
    with pytest.raises(ProviderError) as error:
        run(s)
    assert error.value.kind == "timeout"
    assert len(s.calls) == 1 and rows(s)[0]["error_kind"] == "timeout"
    assert not s.search.jobs


def test_cancelled_request_does_not_switch_or_leave_pending_reservation(s):
    async def scenario():
        entered = asyncio.Event()

        async def handler(_):
            entered.set()
            await asyncio.Event().wait()

        transport(s, handler)
        task = asyncio.create_task(s.search.search(SearchRequest(query="公开主题")))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(s.calls) == 1 and not s.search.jobs
    assert rows(s)[0]["status"] == "uncertain" and rows(s)[0]["error_kind"] == "cancelled"
    assert rows(s)[0]["cost_cny"] == 0.1


def test_config_change_before_fallback_prevents_next_attempt(s):
    def handler(_):
        s.config.current.search.fallbacks["fallback_1"].enabled = False
        return httpx.Response(503)

    transport(s, handler)
    with pytest.raises(ProviderError) as error:
        run(s)
    assert error.value.kind == "not_configured"
    assert len(s.calls) == 1 and len(rows(s)) == 1


def test_pause_before_exploration_fallback_prevents_next_attempt(s):
    s.config.current.exploration.enabled = True

    def handler(_):
        s.config.current.automation.paused = True
        return httpx.Response(503)

    transport(s, handler)
    with pytest.raises(ProviderError) as error:
        run(s, purpose="exploration")
    assert error.value.kind == "permission" and len(s.calls) == 1


def test_frequency_limit_counts_each_attempt(s):
    s.config.current.search.max_calls_per_minute = 1
    transport(s, lambda _: httpx.Response(503))
    with pytest.raises(ProviderError) as error:
        run(s)
    assert error.value.kind == "rate_limit" and len(s.calls) == 1 and len(rows(s)) == 1


def test_single_exploration_can_fail_over_without_consuming_second_task(s):
    s.config.current.exploration.enabled = True
    s.config.current.exploration.max_daily_tasks = 1
    transport(s, lambda r: valid() if r.url.host != "primary.example" else httpx.Response(503))
    assert run(s, purpose="exploration")["provider_id"] == "fallback_1"
    with pytest.raises(ProviderError) as error:
        run(s, purpose="exploration")
    assert error.value.kind == "rate_limit" and len(s.calls) == 2
    assert len({r["request_id"] for r in rows(s)}) == 1


def test_success_diagnostics_are_invalidated_by_endpoint_edits(s):
    transport(s, lambda _: valid())
    run(s, only="fallback_1")
    assert s.search.provider_status()["items"][1]["state"] == "available"
    s.config.current.search.fallbacks["fallback_1"].base_url = "https://new.example"
    assert s.search.provider_status()["items"][1]["state"] == "not_tested"
    s.config.current.search.enabled = False
    assert all(item["state"] == "disabled" for item in s.search.provider_status()["items"])


def test_probe_targets_only_selected_endpoint_even_when_it_fails(s):
    transport(s, lambda _: httpx.Response(503))
    with pytest.raises(ProviderError):
        run(s, only="fallback_1")
    assert s.calls == ["backup-one.example"]
    assert rows(s)[0]["provider"] == "fallback_1"
    with pytest.raises(ProviderError):
        run(s, only="other")
    s.config.current.search.fallbacks["fallback_1"].enabled = False
    with pytest.raises(ProviderError):
        run(s, only="fallback_1")
    assert len(s.calls) == 1


def test_shutdown_cancels_active_search_and_rejects_new_requests(s):
    async def scenario():
        entered = asyncio.Event()

        async def handler(_):
            entered.set()
            await asyncio.Event().wait()

        transport(s, handler)
        task = asyncio.create_task(s.search.search(SearchRequest(query="公开主题")))
        await entered.wait()
        await s.search.close()
        assert task.cancelled() and not s.search.jobs
        with pytest.raises(ProviderError):
            await s.search.search(SearchRequest(query="公开主题"))

    asyncio.run(scenario())
    assert len(s.calls) == 1 and rows(s)[0]["error_kind"] == "cancelled"


def test_api_protects_status_and_test_requires_explicit_confirmation(s):
    transport(s, lambda _: valid())
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as c:
        assert c.get("/api/search/providers").status_code == 401
        assert (
            c.post("/api/search/providers/primary/test", json={"confirm": True}).status_code == 401
        )
        c.post(
            "/api/auth/setup", json={"username": "tester", "password": "search-fixture-password"}
        ).raise_for_status()
        for confirm in [False, "true", 1]:
            assert (
                c.post("/api/search/providers/primary/test", json={"confirm": confirm}).status_code
                == 422
            )
        assert not s.calls
        assert c.post("/api/search/providers/other/test", json={"confirm": True}).status_code == 404
        response = c.post("/api/search/providers/fallback_2/test", json={"confirm": True})
        assert response.status_code == 200 and response.json()["provider_id"] == "fallback_2"
        assert s.calls == ["backup-two.example"]
        assert c.get("/api/search/providers").json()["items"][2]["state"] == "available"
        assert rows(s)[0]["purpose"] == "connection_test"


def test_api_error_keeps_safe_diagnostics_without_echoing_response_body(s):
    transport(s, lambda _: httpx.Response(500, text="private-upstream-token"))
    with TestClient(create_web_app(s.root, s), headers={"X-Soulbot-Request": "1"}) as c:
        c.post(
            "/api/auth/setup", json={"username": "tester", "password": "search-fixture-password"}
        ).raise_for_status()
        response = c.post("/api/search", json={"query": "公开主题"})
        assert response.status_code == 502 and "private-upstream-token" not in response.text
        data = c.get("/api/search/providers").text
        assert "private-upstream-token" not in data and "公开主题" not in data
        assert len(rows(s)) == 3


def test_schema_twenty_one_upgrade_preserves_legacy_usage_and_empty_fallbacks(s):
    with s.db.connect() as db:
        db.execute("DROP TABLE search_attempts")
        db.execute(
            "INSERT INTO search_usage VALUES ('legacy','2026-11-08','explicit','completed',0.8,123)"
        )
        db.execute("UPDATE metadata SET value='21' WHERE key='schema_version'")
    again = Database(s.db.path)
    Database(s.db.path)
    assert again.get("schema_version") == "22"
    assert rows(s)[0]["cost_cny"] == 0.8
    assert s.search.provider_status()["attempts"][0]["provider"] == "primary"
    assert Settings.model_validate({"search": {"enabled": True}}).search.fallbacks == {}


def test_backup_restore_preserves_newer_attempts_and_exploration_task_counts(s):
    s.config.current.exploration.enabled = True
    s.config.current.exploration.max_daily_tasks = 1
    s.config.save(s.config.current)
    backup = BackupService(s.root)
    snapshot = backup.snapshot()
    transport(s, lambda r: valid() if r.url.host != "primary.example" else httpx.Response(503))
    run(s, purpose="exploration")
    backup.stage(snapshot)
    with InstanceLock(s.root) as lock:
        backup.apply_pending(lock)
    again = build_services(s.root)
    assert len(rows(again)) == 2 and len({r["request_id"] for r in rows(again)}) == 1
    assert [r["cost_cny"] for r in rows(again)] == [0.1, None]
    again.config.current.automation.paused = False
    again.calls = []
    transport(again, lambda _: valid())
    with pytest.raises(ProviderError):
        run(again, purpose="exploration")
    assert not again.calls


def test_routes_can_run_concurrently_without_crossing_settings_or_attempt_ids(s):
    transport(s, lambda r: valid() if r.url.host != "primary.example" else httpx.Response(503))

    async def scenario():
        return await asyncio.gather(
            *[s.search.search(SearchRequest(query="公开主题")) for _ in range(2)]
        )

    assert all(r["provider_id"] == "fallback_1" for r in asyncio.run(scenario()))
    data = rows(s)
    assert len(data) == 4 and len({r["request_id"] for r in data}) == 2
    assert not s.search.jobs


@pytest.mark.parametrize("probe", [False, True])
def test_disconnect_cancels_api_attempt_and_never_switches(s, probe):
    from types import SimpleNamespace

    from fastapi import HTTPException

    async def handler(_):
        await asyncio.Event().wait()

    async def disconnected():
        return True

    transport(s, handler)
    app = create_web_app(s.root, s)
    path = "/api/search/providers/{name}/test" if probe else "/api/search"
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == path)

    async def scenario():
        with pytest.raises(HTTPException) as error:
            if probe:
                from soulbot.services.search_service import SearchProbe

                await endpoint(
                    "primary",
                    SearchProbe(confirm=True),
                    SimpleNamespace(is_disconnected=disconnected),
                )
            else:
                await endpoint(
                    SearchRequest(query="公开主题"), SimpleNamespace(is_disconnected=disconnected)
                )
        assert error.value.status_code == 499

    asyncio.run(scenario())
    assert len(rows(s)) == 1 and rows(s)[0]["error_kind"] == "cancelled"
    assert len(s.calls) == 1 and not s.search.jobs
