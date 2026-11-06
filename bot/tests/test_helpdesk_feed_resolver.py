"""Feed-id cache and resolver tests; the browser itself is mocked."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from vikingbot.extensions.feishu_helpdesk.feed_resolver import (
    FeedIdCache,
    FeedResolutionError,
    FeishuBrowserSession,
    FeishuWebFeedResolver,
)


def settings(**overrides):
    values = {
        "feed_ids": {},
        "auto_resolve_feed_id": True,
        "web_url": "https://messenger.test/",
        "browser_executable": "",
        "feed_resolution_timeout_seconds": 12,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_resolver_uses_static_mapping_without_browser(tmp_path, monkeypatch):
    resolver = FeishuWebFeedResolver(settings(feed_ids={"oc_chat": "42"}), cache_path=tmp_path / "ids.json")
    browser = AsyncMock()
    monkeypatch.setattr(
        "vikingbot.extensions.feishu_helpdesk.feed_resolver._resolve_with_browser", browser
    )
    assert await resolver.resolve("oc_chat") == "42"
    browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolver_persists_browser_result_and_reuses_it(tmp_path, monkeypatch):
    path = tmp_path / "ids.json"
    api = SimpleNamespace(
        chat=AsyncMock(
            return_value={
                "name": "[Processing] 45803 Echo.Tang's Supply Chain QA",
                "i18n_names": {"zh_cn": "[处理中] 45803 Echo.Tang的MMT系统问问"},
            }
        )
    )
    resolver = FeishuWebFeedResolver(
        settings(), api=api, cache_path=path, cookie_provider=AsyncMock(return_value=[])
    )
    captured = {}
    resolver._ensure_browser = AsyncMock()

    def resolve_browser(names):
        captured["names"] = names
        return "7704147668575636511"

    resolver._browser = SimpleNamespace(resolve=resolve_browser, close=lambda: None)
    assert await resolver.resolve("oc_chat", cookies=[{"name": "session"}]) == "7704147668575636511"
    assert captured["names"] == [
        "[Processing] 45803 Echo.Tang's Supply Chain QA",
        "[处理中] 45803 Echo.Tang的MMT系统问问",
    ]
    assert FeedIdCache(path).get("oc_chat") == "7704147668575636511"

    api.chat.reset_mock()
    assert await resolver.resolve("oc_chat") == "7704147668575636511"
    api.chat.assert_not_awaited()
    await resolver.close()


@pytest.mark.asyncio
async def test_resolver_requires_opt_in_on_cache_miss(tmp_path):
    resolver = FeishuWebFeedResolver(
        settings(auto_resolve_feed_id=False), cache_path=tmp_path / "ids.json"
    )
    with pytest.raises(RuntimeError, match="not cached"):
        await resolver.resolve("oc_missing")


def test_feed_sync_waits_for_messenger_stores_before_freezing(monkeypatch):
    session = FeishuBrowserSession(
        web_url="https://messenger.test/", cookies=[], executable="", timeout=2
    )
    session._cdp = object()
    attempts = []

    def evaluate(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) < 3:
            raise FeedResolutionError("messenger UI is not ready")
        return [{"feedId": "42"}]

    monkeypatch.setattr(
        "vikingbot.extensions.feishu_helpdesk.feed_resolver._evaluate", evaluate
    )
    monkeypatch.setattr(
        "vikingbot.extensions.feishu_helpdesk.feed_resolver.time.sleep", lambda _: None
    )

    assert session._wait_for_feed_sync(time.monotonic() + 2) == [{"feedId": "42"}]
    assert len(attempts) == 3
    assert all(call["await_promise"] is True for call in attempts)


@pytest.mark.asyncio
async def test_resolver_rebuilds_a_browser_after_a_feed_resolution_failure(
    tmp_path, monkeypatch
):
    api = SimpleNamespace(chat=AsyncMock(return_value={"name": "[Processing] ticket"}))
    resolver = FeishuWebFeedResolver(
        settings(), api=api, cache_path=tmp_path / "ids.json"
    )
    first = SimpleNamespace(
        resolve=Mock(side_effect=FeedResolutionError("half initialized")),
        close=Mock(),
    )
    second = SimpleNamespace(resolve=Mock(return_value="42"), close=Mock())
    browsers = [first, second]

    async def ensure_browser(cookies):
        resolver._browser = browsers.pop(0)

    resolver._ensure_browser = AsyncMock(side_effect=ensure_browser)

    assert await resolver.resolve("oc_chat", cookies=[{"name": "session"}]) == "42"
    first.close.assert_called_once_with()
    second.close.assert_not_called()
    assert FeedIdCache(tmp_path / "ids.json").get("oc_chat") == "42"
    await resolver.close()
