from datetime import datetime, timedelta

from vikingbot.agent.media_window import select_recent_media, session_media_fields
from vikingbot.bus.events import InboundMessage
from vikingbot.config.schema import FeishuMediaWindowConfig, SessionKey
from vikingbot.session.manager import Session

SESSION_KEY = SessionKey(type="feishu", channel_id="cli_bot", chat_id="oc_chat")
NOW = datetime(2026, 11, 5, 10, 0, 0)


def inbound(*, message_id="om_current", sender="ou_user", reply_to=None, media=None, refs=None):
    return InboundMessage(
        session_key=SESSION_KEY,
        sender_id=sender,
        content="@bot 看一下",
        media=media or [],
        media_refs=refs or [],
        metadata={"message_id": message_id, "reply_to": reply_to},
    )


def add_media_message(session, *, message_id, sender, when, refs):
    session.add_message(
        "user",
        "图片消息",
        sender_id=sender,
        message_id=message_id,
        media_refs=refs,
        timestamp=when.isoformat(),
    )


def ref(message_id, key, path, index=0):
    return {
        "source": "feishu_im",
        "message_id": message_id,
        "image_key": key,
        "index": index,
        "cache_path": path,
    }


def test_selects_all_images_from_recent_same_sender_messages():
    session = Session(SESSION_KEY)
    add_media_message(
        session,
        message_id="om_images",
        sender="ou_user",
        when=NOW - timedelta(minutes=1),
        refs=[ref("om_images", "img_1", "/tmp/one.png"), ref("om_images", "img_2", "/tmp/two.png")],
    )

    selected = select_recent_media(
        session,
        inbound(),
        FeishuMediaWindowConfig(max_images=4, max_age_minutes=10, max_messages=20),
        now=NOW.timestamp(),
    )

    assert selected == ["/tmp/one.png", "/tmp/two.png"]


def test_selects_images_split_across_separate_messages():
    session = Session(SESSION_KEY)
    add_media_message(
        session,
        message_id="om_first",
        sender="ou_user",
        when=NOW - timedelta(minutes=2),
        refs=[ref("om_first", "img_1", "/tmp/first.png")],
    )
    add_media_message(
        session,
        message_id="om_second",
        sender="ou_user",
        when=NOW - timedelta(minutes=1),
        refs=[ref("om_second", "img_2", "/tmp/second.png")],
    )

    selected = select_recent_media(
        session,
        inbound(),
        FeishuMediaWindowConfig(max_images=4, max_age_minutes=10, max_messages=20),
        now=NOW.timestamp(),
    )

    assert selected == ["/tmp/second.png", "/tmp/first.png"]


def test_reply_parent_is_used_even_outside_normal_window():
    session = Session(SESSION_KEY)
    add_media_message(
        session,
        message_id="om_parent",
        sender="ou_user",
        when=NOW - timedelta(hours=2),
        refs=[ref("om_parent", "img_parent", "/tmp/parent.png")],
    )
    session.messages.extend(
        {
            "role": "assistant",
            "content": f"message {index}",
            "timestamp": (NOW - timedelta(minutes=30 - index)).isoformat(),
        }
        for index in range(25)
    )

    selected = select_recent_media(
        session,
        inbound(reply_to="om_parent"),
        FeishuMediaWindowConfig(max_images=4, max_age_minutes=10, max_messages=20),
        now=NOW.timestamp(),
    )

    assert selected == ["/tmp/parent.png"]


def test_different_sender_is_not_included_by_default():
    session = Session(SESSION_KEY)
    add_media_message(
        session,
        message_id="om_other",
        sender="ou_other",
        when=NOW - timedelta(minutes=1),
        refs=[ref("om_other", "img_other", "/tmp/other.png")],
    )

    selected = select_recent_media(
        session,
        inbound(),
        FeishuMediaWindowConfig(max_images=4, max_age_minutes=10, max_messages=20),
        now=NOW.timestamp(),
    )

    assert selected == []


def test_current_media_is_first_and_deduplicated():
    session = Session(SESSION_KEY)
    add_media_message(
        session,
        message_id="om_images",
        sender="ou_user",
        when=NOW - timedelta(minutes=1),
        refs=[ref("om_images", "img_1", "/tmp/one.png")],
    )
    current = inbound(
        media=["/tmp/current.png", "/tmp/one.png"],
        refs=[ref("om_current", "img_current", "/tmp/current.png")],
    )

    selected = select_recent_media(
        session,
        current,
        FeishuMediaWindowConfig(max_images=4, max_age_minutes=10, max_messages=20),
        now=NOW.timestamp(),
    )

    assert selected == ["/tmp/current.png", "/tmp/one.png"]


def test_large_images_are_not_size_filtered():
    session = Session(SESSION_KEY)
    path = "/tmp/ten-megabyte-image.png"
    add_media_message(
        session,
        message_id="om_large",
        sender="ou_user",
        when=NOW - timedelta(minutes=1),
        refs=[ref("om_large", "img_large", path)],
    )

    selected = select_recent_media(
        session,
        inbound(),
        FeishuMediaWindowConfig(max_images=1, max_age_minutes=10, max_messages=20),
        now=NOW.timestamp(),
    )

    assert selected == [path]


def test_session_fields_preserve_source_identity_without_entering_history():
    message = inbound(
        media=["/tmp/one.png"],
        refs=[ref("om_current", "img_1", "/tmp/one.png")],
    )
    session = Session(SESSION_KEY)
    session.add_message(
        "user", message.content, sender_id=message.sender_id, **session_media_fields(message)
    )

    assert session.messages[0]["message_id"] == "om_current"
    assert session.messages[0]["media_refs"][0]["image_key"] == "img_1"
    assert session.messages[0]["media_paths"] == ["/tmp/one.png"]
    assert session.get_history()[0] == {"role": "user", "content": message.content}
