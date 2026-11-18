import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vikingbot.bus.queue import MessageBus
from vikingbot.channels.feishu import FeishuChannel
from vikingbot.config.schema import FeishuChannelConfig, FeishuMediaWindowConfig

FIXTURES = Path(__file__).parent / "fixtures"


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


def load_merge_forward_fixture():
    payload = json.loads((FIXTURES / "feishu_merge_forward_response.json").read_text())
    messages = []
    for item in payload["items"]:
        sender = item.get("sender") or {}
        messages.append(
            SimpleNamespace(
                message_id=item["message_id"],
                msg_type=item["msg_type"],
                body=SimpleNamespace(content=item["body"]["content"]),
                sender=SimpleNamespace(
                    id=sender.get("id", ""),
                    sender_name=sender.get("sender_name", ""),
                ),
                create_time=item["create_time"],
                chat_id=item["chat_id"],
                root_id=item.get("root_id", ""),
                thread_id=item.get("thread_id", ""),
                upper_message_id=item.get("upper_message_id", ""),
            )
        )
    return messages


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


def test_media_and_quote_image_limits_are_separate():
    assert FeishuMediaWindowConfig().max_images == 4
    assert FeishuChannelConfig().quote_max_images == 10
    assert FeishuChannelConfig().turn_max_images == 10


@pytest.mark.asyncio
async def test_quoted_text_parent_is_added_without_neighbors():
    channel = make_channel()
    parent = make_api_message(
        "om_parent",
        content={"text": "body_bytes_sent:3579\nhost:hk-wms.wangoon.cn\nrequest_path:/wms-out2b"},
    )
    channel._fetch_message_items = AsyncMock(return_value=[parent])
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
    channel._fetch_message_items = AsyncMock(return_value=[parent])
    channel._list_messages_near = AsyncMock(return_value=[parent, *siblings])
    channel._get_user_name = AsyncMock(return_value="Mack.Luo")

    async def parse(
        message, _msg_type, message_id, *, raw_content=None, max_images=None,
        resource_message_id=None,
    ):
        return "[image]", [f"/tmp/{message_id}.png"], []

    channel._parse_message_content = AsyncMock(side_effect=parse)

    context = await channel._build_quoted_context(
        SimpleNamespace(parent_id="om_parent", chat_id="oc_chat")
    )

    assert context["image_count"] == 10
    assert len(context["media"]) == 10
    assert "/tmp/om_parent.png" in context["media"]


@pytest.mark.asyncio
async def test_contiguous_segment_stops_at_another_sender():
    channel = make_channel()
    parent_time = 1_794_976_260_000
    parent = make_api_message(
        "om_parent",
        msg_type="image",
        content={"image_key": "img_parent"},
        create_time=parent_time,
    )
    same_before = make_api_message(
        "om_before",
        msg_type="image",
        content={"image_key": "img_before"},
        create_time=parent_time - 1000,
    )
    other_sender = make_api_message(
        "om_other",
        msg_type="image",
        content={"image_key": "img_other"},
        sender_id="ou_other",
        sender_name="Other",
        create_time=parent_time + 1000,
    )
    same_after_interruption = make_api_message(
        "om_after",
        msg_type="image",
        content={"image_key": "img_after"},
        create_time=parent_time + 2000,
    )
    channel._fetch_message_items = AsyncMock(return_value=[parent])
    channel._list_messages_near = AsyncMock(
        return_value=[same_before, parent, other_sender, same_after_interruption]
    )
    channel._get_user_name = AsyncMock(return_value="Mack.Luo")

    async def parse(
        message, _msg_type, message_id, *, raw_content=None, max_images=None,
        resource_message_id=None,
    ):
        return "[image]", [f"/tmp/{message_id}.png"], []

    channel._parse_message_content = AsyncMock(side_effect=parse)

    context = await channel._build_quoted_context(
        SimpleNamespace(parent_id="om_parent", chat_id="oc_chat")
    )

    assert "/tmp/om_before.png" in context["media"]
    assert "/tmp/om_parent.png" in context["media"]
    assert "/tmp/om_other.png" not in context["media"]
    assert "/tmp/om_after.png" not in context["media"]


@pytest.mark.asyncio
async def test_merge_forward_reads_child_messages():
    channel = make_channel()
    parent = make_api_message(
        "om_parent",
        msg_type="merge_forward",
        content={"content": "Merged and Forwarded Message"},
    )
    first = make_api_message(
        "om_child_1",
        content={"text": "第一段会话"},
        sender_id="ou_a",
        sender_name="Alice",
        create_time=1_794_976_261_000,
    )
    second = make_api_message(
        "om_child_2",
        msg_type="image",
        content={"image_key": "img_child"},
        sender_id="ou_b",
        sender_name="Bob",
        create_time=1_794_976_262_000,
    )
    channel._fetch_message_items = AsyncMock(return_value=[parent, first, second])
    channel._get_user_name = AsyncMock(side_effect=lambda sender_id, chat_id=None: sender_id)
    resource_message_ids = []

    async def parse(
        message, msg_type, message_id, *, raw_content=None, max_images=None,
        resource_message_id=None,
    ):
        resource_message_ids.append(resource_message_id)
        if msg_type == "image":
            return "[image]", [f"/tmp/{message_id}.png"], []
        return json.loads(raw_content)["text"], [], []

    channel._parse_message_content = AsyncMock(side_effect=parse)

    context = await channel._build_quoted_context(
        SimpleNamespace(parent_id="om_parent", chat_id="oc_chat")
    )

    assert "Quoted conversation record" in context["text"]
    assert "第一段会话" in context["text"]
    assert "Bob" in context["text"]
    assert context["media"] == ["/tmp/om_child_2.png"]
    assert "om_parent" in resource_message_ids


@pytest.mark.asyncio
async def test_real_merge_forward_fixture_preserves_shape_and_root_resource_id():
    channel = make_channel()
    fixture_messages = load_merge_forward_fixture()
    root = fixture_messages[0]
    image_child = fixture_messages[1]
    channel._fetch_message_items = AsyncMock(return_value=fixture_messages)
    parse_calls = []

    async def parse(
        message, msg_type, message_id, *, raw_content=None, max_images=None,
        resource_message_id=None,
    ):
        parse_calls.append((message_id, resource_message_id))
        if msg_type == "image":
            return "[image]", ["/tmp/fixture-image.png"], []
        return json.loads(raw_content)["text"], [], []

    channel._parse_message_content = AsyncMock(side_effect=parse)

    context = await channel._build_quoted_context(
        SimpleNamespace(
            parent_id=root.message_id,
            chat_id=root.chat_id,
            message_id="om_current",
        ),
        max_images=10,
    )

    assert "Quoted conversation record: 4 messages" in context["text"]
    assert "供应商变更" in context["text"]
    assert context["media"] == ["/tmp/fixture-image.png"]
    assert (image_child.message_id, root.message_id) in parse_calls


@pytest.mark.asyncio
async def test_nested_merge_forward_is_expanded_with_its_own_resource_root():
    channel = make_channel()
    outer = make_api_message(
        "om_outer",
        msg_type="merge_forward",
        content={"content": "Merged and Forwarded Message"},
        create_time=1_794_976_300_000,
    )
    nested = make_api_message(
        "om_nested",
        msg_type="merge_forward",
        content={"content": "Merged and Forwarded Message"},
        create_time=1_794_976_301_000,
    )
    outer_text = make_api_message(
        "om_outer_text",
        content={"text": "外层文本"},
        create_time=1_794_976_302_000,
    )
    nested_text = make_api_message(
        "om_nested_text",
        content={"text": "嵌套文本"},
        create_time=1_794_976_303_000,
    )
    nested_image = make_api_message(
        "om_nested_image",
        msg_type="image",
        content={"image_key": "img_nested"},
        create_time=1_794_976_304_000,
    )

    async def fetch(message_id):
        if message_id == "om_outer":
            return [outer, nested, outer_text]
        if message_id == "om_nested":
            return [nested, nested_text, nested_image]
        return []

    parse_calls = []

    async def parse(
        message, msg_type, message_id, *, raw_content=None, max_images=None,
        resource_message_id=None,
    ):
        parse_calls.append((message_id, resource_message_id))
        if msg_type == "image":
            return "[image]", [f"/tmp/{message_id}.png"], []
        return json.loads(raw_content)["text"], [], []

    channel._fetch_message_items = AsyncMock(side_effect=fetch)
    channel._parse_message_content = AsyncMock(side_effect=parse)

    context = await channel._build_quoted_context(
        SimpleNamespace(
            parent_id="om_outer",
            chat_id="oc_chat",
            message_id="om_current",
        ),
        max_images=10,
    )

    assert "Nested merge_forward" in context["text"]
    assert "外层文本" in context["text"]
    assert "嵌套文本" in context["text"]
    assert context["media"] == ["/tmp/om_nested_image.png"]
    assert ("om_nested_image", "om_nested") in parse_calls


@pytest.mark.asyncio
async def test_thread_context_reads_thread_messages():
    channel = make_channel()
    parent = make_api_message(
        "om_parent",
        content={"text": "问题起点"},
        thread_id="omt_thread",
    )
    reply = make_api_message(
        "om_reply",
        content={"text": "后续补充"},
        sender_id="ou_other",
        sender_name="Other",
        create_time=1_794_976_261_000,
        thread_id="omt_thread",
    )
    current = make_api_message(
        "om_current",
        content={"text": "当前问题不应重复"},
        create_time=1_794_976_262_000,
        thread_id="omt_thread",
    )
    channel._fetch_message_items = AsyncMock(return_value=[parent])
    channel._list_thread_messages = AsyncMock(return_value=[parent, reply, current])

    async def parse(
        message, _msg_type, message_id, *, raw_content=None, max_images=None,
        resource_message_id=None,
    ):
        return json.loads(raw_content)["text"], [], []

    channel._parse_message_content = AsyncMock(side_effect=parse)

    context = await channel._build_quoted_context(
        SimpleNamespace(
            parent_id="om_parent",
            chat_id="oc_chat",
            message_id="om_current",
        )
    )

    assert "Quoted thread" in context["text"]
    assert "问题起点" in context["text"]
    assert "后续补充" in context["text"]
    assert "当前问题不应重复" not in context["text"]


def test_bounded_record_window_is_centered_on_anchor():
    channel = make_channel()
    items = [
        make_api_message(
            f"om_{index}",
            content={"text": str(index)},
            create_time=1_794_976_260_000 + index * 1000,
        )
        for index in range(30)
    ]

    selected = channel._bounded_record_window(
        items,
        anchor_id="om_15",
        limit=20,
    )

    assert [item.message_id for item in selected] == [
        f"om_{index}" for index in range(5, 25)
    ]


def test_merge_forward_window_keeps_head_and_tail():
    channel = make_channel()
    items = [
        make_api_message(
            f"om_{index}",
            content={"text": str(index)},
            create_time=1_794_976_260_000 + index * 1000,
        )
        for index in range(30)
    ]

    selected, omitted = channel._merge_forward_record_window(items, limit=10)

    assert [item.message_id for item in selected] == [
        "om_0",
        "om_1",
        "om_2",
        "om_3",
        "om_4",
        "om_25",
        "om_26",
        "om_27",
        "om_28",
        "om_29",
    ]
    assert omitted == 20


@pytest.mark.asyncio
async def test_parse_message_content_marks_omitted_images():
    channel = make_channel()
    image_keys = [f"img_{index}" for index in range(12)]
    message = make_api_message(
        "om_post",
        msg_type="post",
        content={"content": [[{"tag": "img", "image_key": key} for key in image_keys]]},
    )
    channel._download_and_save_image = AsyncMock(
        side_effect=lambda key, message_id: f"/tmp/{key}.png"
    )

    content, media, refs = await channel._parse_message_content(
        message,
        "post",
        "om_post",
        max_images=10,
    )

    assert "[Images omitted: 2; limit 10 reached]" in content
    assert len(media) == 10
    assert len(refs) == 10


@pytest.mark.asyncio
async def test_thread_context_marks_omitted_messages():
    channel = make_channel()
    parent = make_api_message(
        "om_parent",
        content={"text": "问题起点"},
        thread_id="omt_thread",
    )
    replies = [
        make_api_message(
            f"om_reply_{index}",
            content={"text": f"回复 {index}"},
            create_time=1_794_976_261_000 + index * 1000,
            thread_id="omt_thread",
        )
        for index in range(24)
    ]
    channel._fetch_message_items = AsyncMock(return_value=[parent])
    channel._list_thread_messages = AsyncMock(return_value=[parent, *replies])

    async def parse(message, msg_type, message_id, *, raw_content=None, max_images=None,
                    resource_message_id=None):
        return json.loads(raw_content)["text"], [], []

    channel._parse_message_content = AsyncMock(side_effect=parse)

    context = await channel._build_quoted_context(
        SimpleNamespace(
            parent_id="om_parent",
            chat_id="oc_chat",
            message_id="om_current",
        )
    )

    assert "Quoted thread: 20 messages, 5 messages omitted" in context["text"]


def test_contiguous_segment_excludes_current_message():
    channel = make_channel()
    parent = make_api_message(
        "om_parent",
        msg_type="image",
        content={"image_key": "img_parent"},
    )
    current = make_api_message(
        "om_current",
        msg_type="image",
        content={"image_key": "img_current"},
        create_time=1_794_976_261_000,
    )

    segment = channel._contiguous_same_sender_segment(
        parent,
        [parent, current],
        exclude_ids={"om_current"},
    )

    assert [item.message_id for item in segment] == ["om_parent"]


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
    assert kwargs["metadata"]["quoted_media_paths"] == ["/tmp/quoted.png"]


@pytest.mark.asyncio
async def test_on_message_enforces_ten_image_turn_limit():
    channel = make_channel()
    quoted_media = [f"/tmp/quoted-{index}.png" for index in range(12)]
    channel._build_quoted_context = AsyncMock(
        return_value={
            "text": "[Quoted conversation record]\nrecord\n[End quoted conversation record]",
            "media": quoted_media,
            "parent_id": "om_parent",
            "image_count": len(quoted_media),
        }
    )
    channel._handle_message = AsyncMock()
    channel._get_user_name = AsyncMock(return_value="Thunder.Wu")
    channel._check_should_process = AsyncMock(return_value=True)
    channel._add_reaction = AsyncMock()
    channel._get_chat_mode = AsyncMock(return_value="group")

    await channel._on_message(inbound_event())

    kwargs = channel._handle_message.await_args.kwargs
    assert kwargs["media"] == quoted_media[:10]
    assert kwargs["metadata"]["turn_image_count"] == 10
    assert kwargs["metadata"]["turn_image_limit"] == 10
    assert kwargs["metadata"]["quoted_image_count"] == 10
