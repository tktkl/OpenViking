import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vikingbot.bus.queue import MessageBus
from vikingbot.channels.feishu import FeishuChannel
from vikingbot.config.schema import FeishuChannelConfig, FeishuMediaWindowConfig


def make_api_message(
    message_id,
    *,
    content,
    msg_type="text",
    sender_id="ou_mack",
    sender_name="Mack.Luo",
    create_time=1_794_976_260_000,
    chat_id="oc_chat",
    root_id="",
    thread_id="",
):
    raw_content = json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content
    return SimpleNamespace(
        message_id=message_id,
        msg_type=msg_type,
        body=SimpleNamespace(content=raw_content),
        sender=SimpleNamespace(id=sender_id, sender_name=sender_name),
        create_time=create_time,
        chat_id=chat_id,
        root_id=root_id,
        thread_id=thread_id,
    )


def make_channel():
    channel = FeishuChannel(FeishuChannelConfig(app_id="cli_app"), MessageBus())
    channel.bot_open_id = "ou_bot"
    channel._client = object()
    return channel


def inbound_event():
    return SimpleNamespace(
        event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_type="user",
                sender_id=SimpleNamespace(open_id="ou_user"),
            ),
            message=SimpleNamespace(
                message_id="om_current",
                chat_id="oc_chat",
                chat_type="group",
                message_type="text",
                content=json.dumps({"text": "解析这个请求看看是谁"}, ensure_ascii=False),
                root_id="",
                parent_id="om_parent",
                mentions=[],
                create_time=1_794_976_300_000,
            ),
        )
    )


def test_media_window_defaults_to_ten_images():
    assert FeishuMediaWindowConfig().max_images == 10


@pytest.mark.asyncio
async def test_quoted_text_parent_is_added_without_neighbors():
    channel = make_channel()
    parent = make_api_message(
        "om_parent",
        content={"text": "body_bytes_sent:3579\nhost:hk-wms.wangoon.cn\nrequest_path:/wms-out2b"},
    )
    channel._fetch_message = AsyncMock(return_value=parent)
    channel._list_messages_near = AsyncMock(return_value=[])
    channel._get_user_name = AsyncMock(return_value="Mack.Luo")

    context = await channel._build_quoted_context(
        SimpleNamespace(parent_id="om_parent", chat_id="oc_chat")
    )

    assert "Quoted message from Mack.Luo" in context["text"]
    assert "body_bytes_sent:3579" in context["text"]
    assert "request_path:/wms-out2b" in context["text"]
    assert context["media"] == []
    channel._list_messages_near.assert_not_awaited()


@pytest.mark.asyncio
async def test_image_burst_includes_at_most_ten_same_sender_images():
    channel = make_channel()
    parent_time = 1_794_976_260_000
    parent = make_api_message(
        "om_parent",
        msg_type="image",
        content={"image_key": "img_parent"},
        create_time=parent_time,
    )
    siblings = [
        make_api_message(
            f"om_image_{index}",
            msg_type="image",
            content={"image_key": f"img_{index}"},
            create_time=parent_time + (index - 6) * 1000,
        )
        for index in range(12)
    ]
    other_sender = make_api_message(
        "om_other",
        msg_type="image",
        content={"image_key": "img_other"},
        sender_id="ou_other",
        sender_name="Other",
        create_time=parent_time + 1000,
    )
    channel._fetch_message = AsyncMock(return_value=parent)
    channel._list_messages_near = AsyncMock(return_value=[parent, *siblings, other_sender])
    channel._get_user_name = AsyncMock(return_value="Mack.Luo")

    async def parse(message, _msg_type, message_id, *, raw_content=None, max_images=None):
        return "[image]", [f"/tmp/{message_id}.png"], []

    channel._parse_message_content = AsyncMock(side_effect=parse)

    context = await channel._build_quoted_context(
        SimpleNamespace(parent_id="om_parent", chat_id="oc_chat")
    )

    assert context["image_count"] == 10
    assert len(context["media"]) == 10
    assert "/tmp/om_parent.png" in context["media"]
    assert "/tmp/om_other.png" not in context["media"]


@pytest.mark.asyncio
async def test_on_message_prepends_quoted_context_and_metadata():
    channel = make_channel()
    channel._build_quoted_context = AsyncMock(
        return_value={
            "text": "[Quoted message from Mack.Luo]\nrequest log\n[End quoted message]",
            "media": ["/tmp/quoted.png"],
            "parent_id": "om_parent",
            "image_count": 1,
        }
    )
    channel._handle_message = AsyncMock()
    channel._get_user_name = AsyncMock(return_value="Thunder.Wu")
    channel._check_should_process = AsyncMock(return_value=True)
    channel._add_reaction = AsyncMock()
    channel._get_chat_mode = AsyncMock(return_value="group")

    await channel._on_message(inbound_event())

    kwargs = channel._handle_message.await_args.kwargs
    assert "解析这个请求看看是谁" in kwargs["content"]
    assert "[Quoted message from Mack.Luo]" in kwargs["content"]
    assert kwargs["media"] == ["/tmp/quoted.png"]
    assert kwargs["metadata"]["parent_id"] == "om_parent"
    assert kwargs["metadata"]["quoted_message_id"] == "om_parent"
    assert kwargs["metadata"]["quoted_image_count"] == 1
