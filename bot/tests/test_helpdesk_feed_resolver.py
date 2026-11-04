"""Feed-id cache and resolver tests; the browser itself is mocked."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vikingbot.extensions.feishu_helpdesk.feed_resolver import (
    FeedIdCache,
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
