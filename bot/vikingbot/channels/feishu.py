"""Feishu/Lark channel implementation using lark-oapi SDK with WebSocket long connection."""

import asyncio
import io
import json
import re
import threading
import time
from bisect import bisect_left
from collections import OrderedDict
from datetime import datetime
from typing import Any

import httpx
from loguru import logger

from vikingbot.config import load_config
from vikingbot.utils import detect_image_format, get_data_path

# Optional HTML processing libraries
try:
    import html2text
    from bs4 import BeautifulSoup
    from readability import Document

    HTML_PROCESSING_AVAILABLE = True
except ImportError:
    HTML_PROCESSING_AVAILABLE = False
    html2text = None
    BeautifulSoup = None
    Document = None

from vikingbot.bus.events import OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.base import BaseChannel
from vikingbot.channels.feishu_files import FeishuFileSendError, send_files
from vikingbot.config.schema import BotMode, Config, FeishuChannelConfig
from vikingbot.utils.image_format import sniff_image_format
from vikingbot.utils.session_paths import workspace_name

try:
    import lark_oapi as lark
    from lark_oapi.api.contact.v3 import (
        GetUserRequest,
    )
    from lark_oapi.api.im.v1 import (
        CreateMessageReactionRequest,
        CreateMessageReactionRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        Emoji,
        GetChatMembersRequest,
        GetChatRequest,
        GetImageRequest,
        GetMessageRequest,
        GetMessageResourceRequest,
        ListMessageRequest,
        P2ImMessageReceiveV1,
        ReplyMessageRequest,
        ReplyMessageRequestBody,
    )

    FEISHU_AVAILABLE = True
except ImportError:
    FEISHU_AVAILABLE = False
    lark = None
    Emoji = None
    GetImageRequest = None
    GetMessageRequest = None
    GetUserRequest = None
    GetChatMembersRequest = None
    ListMessageRequest = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}

# Pre-compiled regex patterns
OPEN_ID_MENTION_PATTERN = re.compile(r"@ou_[a-f0-9]+")

# Bounded quoted context: parent messages, merged-forward/thread records, or a
# contiguous same-sender media burst with its nearest captions.
QUOTE_CONTEXT_WINDOW_SECONDS = 120
QUOTE_CONTEXT_SEGMENT_GAP_MS = 30_000
QUOTE_CONTEXT_MAX_RECORD_MESSAGES = 20
QUOTE_CONTEXT_MAX_FORWARD_DEPTH = 3
QUOTE_CONTEXT_MAX_CHARS = 6000


class FeishuChannel(BaseChannel):
    """
    Feishu/Lark channel using WebSocket long connection.

    Uses WebSocket to receive events - no public IP or webhook required.

    Requires:
    - App ID and App Secret from Feishu Open Platform
    - Bot capability enabled
    - Event subscription enabled (im.message.receive_v1)
    """

    name = "feishu"
    # 飞书官方支持的处理中表情列表，按顺序发送
    PROCESSING_EMOJIS = [
        "StatusInFlight",
        "OneSecond",
        "Typing",
        "OnIt",
        "Coffee",
        "OnIt",
        "EatingFood",
    ]

    def __init__(
        self,
        config: FeishuChannelConfig,
        bus: MessageBus,
        *,
        bot_config: Config | None = None,
        **kwargs,
    ):
        super().__init__(config, bus, **kwargs)
        self.config: FeishuChannelConfig = config
        self._bot_config = bot_config
        self._client: Any = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # Ordered dedup cache
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tenant_access_token: str | None = None
        self._token_expire_time: float = 0
        self._chat_mode_cache: dict[str, str] = {}  # 缓存群类型：group(普通群)/thread(话题群)
        self._user_name_cache: OrderedDict[str, str] = OrderedDict()  # LRU缓存用户ID到姓名的映射
        self._chat_member_cache: OrderedDict[str, dict[str, Any]] = (
            OrderedDict()
        )  # chat_id -> {members, expires_at, last_error_at}
        self._MAX_USER_CACHE_SIZE = 1000  # 最大缓存1000个用户
        self._CHAT_MEMBER_CACHE_TTL_SEC = 300
        self._CHAT_MEMBER_CACHE_MAX_CHATS = 30
        self._CHAT_MEMBER_FETCH_COOLDOWN_SEC = 60
        self._CHAT_MEMBER_FETCH_PAGE_SIZE = 100
        self._CHAT_MEMBER_FETCH_MAX_PAGES = 500

    async def _get_tenant_access_token(self) -> str:
        """Get tenant access token for Feishu API."""
        now = time.time()
        if (
            self._tenant_access_token and now < self._token_expire_time - 60
        ):  # Refresh 1 min before expire
            return self._tenant_access_token

        url = f"{self.config.domain}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.config.app_id, "app_secret": self.config.app_secret}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") != 0:
                raise Exception(f"Failed to get tenant access token: {result}")

            self._tenant_access_token = result["tenant_access_token"]
            self._token_expire_time = now + result.get("expire", 7200)
            return self._tenant_access_token

    async def _upload_image_to_feishu(self, image_data: bytes) -> str:
        """
        Upload image to Feishu media library and get image_key.
        """

        if not image_data:
            raise ValueError("Feishu image upload requires a non-empty image")

        token = await self._get_tenant_access_token()
        url = f"{self.config.domain}/open-apis/im/v1/images"

        headers = {"Authorization": f"Bearer {token}"}
        data = {"image_type": "message"}

        async def post_image(client: httpx.AsyncClient, upload_data: bytes) -> httpx.Response:
            image_format = detect_image_format(upload_data)
            files = {
                "image": (
                    f"image.{image_format.extension}",
                    io.BytesIO(upload_data),
                    image_format.mime_type,
                )
            }
            return await client.post(url, headers=headers, data=data, files=files)

        async with httpx.AsyncClient(timeout=60.0) as client:
            upload_data = image_data
            resp = await post_image(client, upload_data)

            if resp.status_code == 400:
                normalized = self._normalize_image_for_feishu(image_data)
                if normalized != image_data:
                    logger.warning(
                        "Retrying Feishu image upload after re-encoding image "
                        f"({len(image_data)} -> {len(normalized)} bytes)"
                    )
                    upload_data = normalized
                    resp = await post_image(client, upload_data)

            if resp.is_error:
                raise Exception(
                    "Failed to upload image to Feishu: "
                    f"status={resp.status_code}, body={resp.text}, "
                    f"image_size={len(upload_data)}"
                )

            result = resp.json()
            if result.get("code") != 0:
                raise Exception(
                    f"Failed to upload image to Feishu: response={result}, "
                    f"image_size={len(upload_data)}"
                )
            return result["data"]["image_key"]

    @staticmethod
    def _normalize_image_for_feishu(image_data: bytes) -> bytes:
        """Re-encode an image to strip metadata that Feishu may reject."""
        try:
            from PIL import Image, ImageOps
        except ImportError:
            logger.warning("Pillow is not installed; cannot normalize image for Feishu upload")
            return image_data

        try:
            with Image.open(io.BytesIO(image_data)) as source:
                image = ImageOps.exif_transpose(source)
                if image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    image = Image.alpha_composite(background, rgba).convert("RGB")
                elif image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                else:
                    image = image.copy()

            output = io.BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True)
            normalized = output.getvalue()
            return normalized if normalized else image_data
        except Exception as exc:
            logger.warning(f"Failed to normalize image for Feishu upload: {exc}")
            return image_data

    async def _download_feishu_image(self, image_key: str, message_id: str | None = None) -> bytes:
        """
        Download an image from Feishu using image_key. If message_id is provided,
        uses GetMessageResourceRequest (for user-sent images), otherwise uses GetImageRequest.
        """
        if not self._client:
            raise Exception("Feishu client not initialized")

        if message_id:
            # Use GetMessageResourceRequest for user-sent images
            request: GetMessageResourceRequest = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            response = await self._client.im.v1.message_resource.aget(request)
        else:
            # Use GetImageRequest for bot-sent/images uploaded via API
            request: GetImageRequest = GetImageRequest.builder().image_key(image_key).build()
            response = await self._client.im.v1.image.aget(request)

        # Handle failed response
        if not response.success():
            raw_detail = getattr(getattr(response, "raw", None), "content", response.msg)
            raise Exception(
                f"Failed to download image: code={response.code}, msg={raw_detail}, log_id={response.get_log_id()}"
            )

        # Read the image bytes from the response file
        return response.file.read()

    async def _get_chat_mode(self, chat_id: str) -> str:
        """获取群类型：group(普通群)/thread(话题群)"""
        if chat_id in self._chat_mode_cache:
            return self._chat_mode_cache[chat_id]

        if not self._client:
            return "group"  # 默认普通群

        try:
            request: GetChatRequest = (
                GetChatRequest.builder().chat_id(chat_id).user_id_type("open_id").build()
            )
            response = await self._client.im.v1.chat.aget(request)
            # 处理失败返回
            if not response.success():
                logger.warning(
                    f"client.im.v1.chat.get failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}"
                )
                return "group"

            # 处理业务结果
            data = response.data
            mode = "group"
            group_message_type = getattr(data, "group_message_type", "")
            if group_message_type and group_message_type == "thread":
                mode = "thread"
            else:
                chat_mode = getattr(data, "chat_mode", "")
                if chat_mode and chat_mode == "topic":
                    mode = "thread"
            self._chat_mode_cache[chat_id] = mode
            return mode
        except Exception as e:
            logger.warning(f"Error getting chat mode: {e}")

        return "group"  # 失败默认普通群

    def _is_bot_mention(self, mention) -> bool:
        bot_id = getattr(self, "bot_open_id", None)
        if bot_id:
            return getattr(getattr(mention, "id", None), "open_id", None) == bot_id
        return bool(self.config.bot_name and getattr(mention, "name", "") == self.config.bot_name)

    # 服务台扩展接入：从 start() 提取原有事件注册，让子类在同一分发器追加工单事件，
    # 避免复制整段官方启动流程；基础渠道仍仅注册原有 IM 消息事件。
    def _event_handler_builder(self):
        """Build the IM dispatcher; channel extensions may register additional events."""
        return lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "", self.config.verification_token or ""
        ).register_p2_im_message_receive_v1(self._on_message_sync)

    async def start(self) -> None:
        """Start the Feishu bot with WebSocket long connection."""
        if not FEISHU_AVAILABLE:
            logger.exception(
                "Feishu SDK not installed. Install with: uv pip install lark-oapi>=1.0.0"
            )
            return

        if not self.config.app_id or not self.config.app_secret:
            logger.exception("Feishu app_id and app_secret not configured")
            return

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .domain(self.config.domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )

        # 服务台扩展接入：子类完成追加注册后统一 build，复用原有飞书长连接和消息处理。
        event_handler = self._event_handler_builder().build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler,
            domain=self.config.domain,
            log_level=lark.LogLevel.INFO,
        )

        # Start WebSocket client in a separate thread with reconnect loop
        def run_ws():
            while self._running:
                try:
                    self._ws_client.start()
                except Exception as e:
                    logger.exception(f"Feishu WebSocket error: {e}")
                if self._running:
                    import time

                    time.sleep(5)

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        logger.info("Feishu bot started with WebSocket long connection")
        logger.info("No public IP required - using WebSocket to receive events")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """Stop the Feishu bot."""
        self._running = False
        if self._ws_client:
            try:
                # Try to close the WebSocket connection gracefully
                if hasattr(self._ws_client, "close"):
                    self._ws_client.close()
            except Exception as e:
                logger.debug(f"Error closing WebSocket client: {e}")
        logger.info("Feishu bot stopped")

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> None:
        """Sync helper for adding reaction (runs in thread pool)."""
        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.warning(f"Failed to add reaction: code={response.code}, msg={response.msg}")
        except Exception as e:
            logger.warning(f"Error adding reaction: {e}")

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> None:
        """
        Add a reaction emoji to a message (non-blocking).

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        if not self._client or not Emoji:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    async def send_processing_reaction(self, message_id: str, emoji: str) -> None:
        """
        Send processing reaction emoji implementation for Feishu.
        """
        await self._add_reaction(message_id, emoji)

    async def handle_processing_tick(self, message_id: str, tick_count: int) -> None:
        """
        Handle processing tick event, send corresponding emoji reaction.
        """
        if 0 <= tick_count < len(self.PROCESSING_EMOJIS):
            emoji = self.PROCESSING_EMOJIS[tick_count]
            await self.send_processing_reaction(message_id, emoji)

    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

    @staticmethod
    def _parse_md_table(table_text: str) -> dict | None:
        """Parse a markdown table into a Feishu table element."""
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None

        def split(l: str) -> list[str]:
            return [c.strip() for c in l.strip("|").split("|")]

        headers = split(lines[0])
        rows = [split(l) for l in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """Split content into div/markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        table_count = 0
        max_tables = 5  # Feishu card table limit

        for m in self._TABLE_RE.finditer(content):
            before = content[last_end : m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))

            if table_count < max_tables:
                elements.append(
                    self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)}
                )
                table_count += 1
            else:
                # Exceeded table limit, render as markdown instead
                elements.append({"tag": "markdown", "content": m.group(1)})

            last_end = m.end()

        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))

        return elements or [{"tag": "markdown", "content": content}]

    def _split_headings(self, content: str) -> list[dict]:
        """Split content by headings, converting headings to div elements."""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = m.group(2).strip()
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{text}**",
                    },
                }
            )
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    async def send(self, msg: OutboundMessage) -> bool:
        """Send a message through Feishu."""
        # 先调用基类处理通用动作
        if await super().send(msg):
            return False

        if not self._client:
            logger.warning("Feishu client not initialized")
            return False

        # Only send normal response messages, skip thinking/tool_call/etc.
        if not msg.is_normal_message:
            return False

        try:
            # logger.info(f"Sending message {msg}")
            # Determine receive_id_type based on chat_id format
            # open_id starts with "ou_", chat_id starts with "oc_"
            reply_to = msg.metadata.get("reply_to")
            if not isinstance(reply_to, str) or not reply_to:
                logger.warning(
                    f"Skipping Feishu message without reply_to metadata: session={msg.session_key}"
                )
                return False
            if reply_to.startswith("oc_"):
                receive_id_type = "chat_id"
            else:
                receive_id_type = "open_id"

            reply_to_message_id = self._reply_to_message_id_from_metadata(msg.metadata)
            if msg.files:
                try:
                    await send_files(
                        domain=self.config.domain,
                        access_token=await self._get_tenant_access_token(),
                        receive_id=reply_to,
                        files=msg.files,
                        reply_to_message_id=reply_to_message_id,
                        reply_in_thread=self._should_reply_in_thread(
                            msg.metadata, reply_to_message_id, msg.session_key.chat_id
                        )
                        if reply_to_message_id
                        else False,
                    )
                except FeishuFileSendError as exc:
                    logger.warning(str(exc))
                    return False
                except Exception as exc:
                    logger.warning(f"Failed to send Feishu file attachment: {type(exc).__name__}")
                    return False
                if not (msg.content or "").strip():
                    return True

            # Process images and get cleaned content
            cleaned_content, images = await self._extract_and_upload_images(msg.content, msg)

            content_with_mentions = cleaned_content

            # --- Build interactive card with markdown rendering ---
            original_sender_id = None
            chat_type = "group"
            if msg.metadata:
                original_sender_id = msg.metadata.get("sender_id")
                chat_type = msg.metadata.get("chat_type", "group")

            # Build card elements using markdown for proper formatting
            card_elements: list[dict] = []

            # @mention prefix only when replying in group chats
            mention_prefix = ""
            if reply_to_message_id and original_sender_id and chat_type == "group":
                mention_prefix = f'<at id="{original_sender_id}"></at>'

            if content_with_mentions.strip():
                md_content = (
                    f"{mention_prefix}\n{content_with_mentions}"
                    if mention_prefix
                    else content_with_mentions
                )
                card_elements.extend(self._build_card_elements(md_content))
            elif mention_prefix:
                card_elements.append({"tag": "markdown", "content": mention_prefix})

            # Add images
            for img in images:
                card_elements.append(
                    {
                        "tag": "img",
                        "img_key": img["image_key"],
                        "alt": {"tag": "plain_text", "content": ""},
                    }
                )

            if not card_elements:
                card_elements.append({"tag": "markdown", "content": " "})

            # Build interactive card message
            card_payload = {
                "config": {"wide_screen_mode": True},
                "elements": card_elements,
            }
            card_content = json.dumps(card_payload, ensure_ascii=False)

            if reply_to_message_id:
                # Reply to existing message (quotes the original)
                should_reply_in_thread = self._should_reply_in_thread(
                    msg.metadata, reply_to_message_id, msg.session_key.chat_id
                )

                request = (
                    ReplyMessageRequest.builder()
                    .message_id(reply_to_message_id)
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .content(card_content)
                        .msg_type("interactive")
                        .reply_in_thread(should_reply_in_thread)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.message.reply(request)
            else:
                # Send new message
                request = (
                    CreateMessageRequest.builder()
                    .receive_id_type(receive_id_type)
                    .request_body(
                        CreateMessageRequestBody.builder()
                        .receive_id(reply_to)
                        .msg_type("interactive")
                        .content(card_content)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.message.create(request)

            if not response.success():
                if response.code == 230011:
                    # Original message was withdrawn, just log warning
                    logger.warning(
                        f"Failed to reply to message: original message was withdrawn, code={response.code}, "
                        f"msg={response.msg}, log_id={response.get_log_id()}"
                    )
                else:
                    logger.exception(
                        f"Failed to send Feishu message: code={response.code}, "
                        f"msg={response.msg}, log_id={response.get_log_id()}"
                    )

            return response.success()

        except Exception as e:
            logger.exception(f"Error sending Feishu message: {e}")
            return False

    @staticmethod
    def _reply_to_message_id_from_metadata(metadata: dict[str, Any] | None) -> str | None:
        """Resolve the Feishu message id to reply to, including scheduled thread delivery."""
        if not metadata:
            return None

        for key in ("reply_to_message_id", "message_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return value

        root_id = metadata.get("root_id")
        if metadata.get("chat_mode") == "thread" and isinstance(root_id, str) and root_id:
            return root_id

        return None

    @staticmethod
    def _should_reply_in_thread(
        metadata: dict[str, Any] | None,
        reply_to_message_id: str,
        session_chat_id: str | None = None,
    ) -> bool:
        """Return whether Feishu reply API should create a topic-thread reply."""
        if not metadata:
            return False

        if metadata.get("chat_type") != "group":
            return False

        # Normal group quoted replies can also carry root_id/parent_id. Only topic groups
        # should use reply_in_thread, otherwise Feishu turns a normal quoted reply into a thread.
        is_thread_chat = metadata.get("chat_mode") == "thread" or (
            bool(session_chat_id) and "#" in session_chat_id
        )
        if not is_thread_chat:
            return False

        root_id = metadata.get("root_id")
        return bool(root_id and root_id != reply_to_message_id)

    def _on_message_sync(self, data: "P2ImMessageReceiveV1") -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _download_and_save_image(self, image_key: str, message_id: str) -> str | None:
        """Download single Feishu image and save to local, return file path or None if failed."""
        try:
            logger.info(
                f"Downloading Feishu image with image_key: {image_key}, message_id: {message_id}"
            )
            image_bytes = await self._download_feishu_image(image_key, message_id)
            if not image_bytes:
                logger.warning(f"Could not download image for image_key: {image_key}")
                return None

            media_dir = get_data_path() / "received"
            media_dir.mkdir(parents=True, exist_ok=True)

            import uuid

            file_path = media_dir / f"feishu_{uuid.uuid4().hex[:16]}.png"
            file_path.write_bytes(image_bytes)

            logger.info(f"Feishu image saved to: {file_path}")
            return str(file_path)
        except Exception as e:
            logger.warning(f"Failed to download Feishu image {image_key}: {e}")
            import traceback

            logger.debug(f"Stack trace: {traceback.format_exc()}")
            return None

    @staticmethod
    def _message_type(message: Any) -> str:
        return str(getattr(message, "msg_type", None) or getattr(message, "message_type", "") or "")

    @staticmethod
    def _message_content(message: Any) -> str:
        body = getattr(message, "body", None)
        value = getattr(body, "content", None) if body is not None else None
        return str(value if value is not None else getattr(message, "content", "") or "")

    @staticmethod
    def _message_sender_id(message: Any) -> str:
        sender = getattr(message, "sender", None)
        sender_id = getattr(sender, "sender_id", None) if sender is not None else None
        return str(getattr(sender_id, "open_id", "") or getattr(sender, "id", "") or "")

    @staticmethod
    def _message_sender_name(message: Any) -> str:
        sender = getattr(message, "sender", None)
        return str(
            getattr(sender, "sender_name", "") or getattr(sender, "name", "") or ""
        )

    @staticmethod
    def _message_time_ms(message: Any) -> int:
        try:
            timestamp = int(getattr(message, "create_time", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return timestamp * 1000 if 0 < timestamp < 10_000_000_000 else timestamp

    async def _fetch_message_items(self, message_id: str) -> list[Any]:
        """Fetch a message and, for merge_forward, its child messages."""
        if not self._client or GetMessageRequest is None:
            return []
        try:
            request = (
                GetMessageRequest.builder()
                .message_id(message_id)
                .user_id_type("open_id")
                .with_sender_name(True)
                .build()
            )
            response = await self._client.im.v1.message.aget(request)
            if not response.success():
                logger.warning(
                    f"Failed to fetch quoted Feishu message: message_id={message_id}, "
                    f"code={response.code}, msg={response.msg}"
                )
                return []
            return list(getattr(getattr(response, "data", None), "items", None) or [])
        except Exception as error:
            logger.warning(f"Failed to fetch quoted Feishu message {message_id}: {error}")
            return []

    async def _list_messages_near(self, chat_id: str, center_time_ms: int) -> list[Any]:
        """List a bounded chat-message window around the quoted parent."""
        if not self._client or ListMessageRequest is None or not chat_id or not center_time_ms:
            return []
        center = int(center_time_ms / 1000)
        try:
            request = (
                ListMessageRequest.builder()
                .container_id_type("chat")
                .container_id(chat_id)
                .start_time(str(max(0, center - QUOTE_CONTEXT_WINDOW_SECONDS)))
                .end_time(str(center + QUOTE_CONTEXT_WINDOW_SECONDS))
                .sort_type("ByCreateTimeAsc")
                .page_size(50)
                .with_sender_name(True)
                .card_msg_content_type("raw_card_content")
                .build()
            )
            response = await self._client.im.v1.message.alist(request)
            if not response.success():
                logger.warning(
                    f"Failed to list messages near quoted Feishu message: chat_id={chat_id}, "
                    f"code={response.code}, msg={response.msg}"
                )
                return []
            return list(getattr(getattr(response, "data", None), "items", None) or [])
        except Exception as error:
            logger.warning(f"Failed to list messages near quoted Feishu message: {error}")
            return []

    async def _list_thread_messages(self, thread_id: str) -> list[Any]:
        """Read a bounded topic thread for a quoted thread message."""
        if not self._client or ListMessageRequest is None or not thread_id:
            return []
        try:
            request = (
                ListMessageRequest.builder()
                .container_id_type("thread")
                .container_id(thread_id)
                .sort_type("ByCreateTimeAsc")
                .page_size(50)
                .with_sender_name(True)
                .card_msg_content_type("raw_card_content")
                .build()
            )
            response = await self._client.im.v1.message.alist(request)
            if not response.success():
                logger.warning(
                    f"Failed to list quoted Feishu thread: thread_id={thread_id}, "
                    f"code={response.code}, msg={response.msg}"
                )
                return []
            return list(getattr(getattr(response, "data", None), "items", None) or [])
        except Exception as error:
            logger.warning(f"Failed to list quoted Feishu thread {thread_id}: {error}")
            return []

    @staticmethod
    def _contiguous_same_sender_segment(
        parent: Any,
        nearby: list[Any],
        *,
        exclude_ids: set[str] | None = None,
    ) -> list[Any]:
        """Keep only the same-sender block touching the quoted parent."""
        parent_id = str(getattr(parent, "message_id", "") or "")
        parent_sender = FeishuChannel._message_sender_id(parent)
        parent_time = FeishuChannel._message_time_ms(parent)
        excluded = set(exclude_ids or set())
        excluded.add(parent_id)
        candidates = [
            item
            for item in nearby
            if str(getattr(item, "message_id", "") or "") not in excluded
        ]
        before = sorted(
            (item for item in candidates if FeishuChannel._message_time_ms(item) <= parent_time),
            key=FeishuChannel._message_time_ms,
            reverse=True,
        )
        after = sorted(
            (item for item in candidates if FeishuChannel._message_time_ms(item) > parent_time),
            key=FeishuChannel._message_time_ms,
        )

        segment = [parent]
        previous_time = parent_time
        for item in before:
            item_time = FeishuChannel._message_time_ms(item)
            if (
                FeishuChannel._message_sender_id(item) != parent_sender
                or previous_time - item_time > QUOTE_CONTEXT_SEGMENT_GAP_MS
            ):
                break
            segment.insert(0, item)
            previous_time = item_time

        previous_time = parent_time
        for item in after:
            item_time = FeishuChannel._message_time_ms(item)
            if (
                FeishuChannel._message_sender_id(item) != parent_sender
                or item_time - previous_time > QUOTE_CONTEXT_SEGMENT_GAP_MS
            ):
                break
            segment.append(item)
            previous_time = item_time
        return segment

    async def _sender_label(self, message: Any, chat_id: str) -> str:
        sender_label = self._message_sender_name(message)
        sender_id = self._message_sender_id(message)
        if not sender_label and sender_id:
            sender_label = await self._get_user_name(sender_id, chat_id=chat_id) or sender_id
        return sender_label or "unknown sender"

    @staticmethod
    def _message_time_label(message: Any) -> str:
        timestamp = FeishuChannel._message_time_ms(message)
        if not timestamp:
            return "unknown time"
        return datetime.fromtimestamp(timestamp / 1000).strftime("%Y-%m-%d %H:%M")

    async def _build_quoted_parent_context(self, message: Any) -> dict[str, Any] | None:
        """Read only the quoted parent, without thread or media-burst expansion."""
        parent_id = str(getattr(message, "parent_id", "") or "")
        if not parent_id:
            return None
        items = await self._fetch_message_items(parent_id)
        if not items:
            return None
        parent = next(
            (item for item in items if str(getattr(item, "message_id", "") or "") == parent_id),
            items[0],
        )
        parent_type = self._message_type(parent)
        chat_id = str(getattr(parent, "chat_id", "") or getattr(message, "chat_id", ""))
        image_limit = max(1, int(getattr(self.config, "quote_max_images", 10)))
        parent_text, parent_media, _ = await self._parse_message_content(
            parent,
            parent_type,
            parent_id,
            raw_content=self._message_content(parent),
            max_images=image_limit,
        )
        parent_text = parent_text.strip()
        if parent_text == "[image]":
            parent_text = ""
        sender_label = await self._sender_label(parent, chat_id)
        parts = [
            f"[Quoted message from {sender_label} at {self._message_time_label(parent)}]"
        ]
        if parent_text:
            parts.append(parent_text)
        if parent_media:
            parts.append(
                f"[Quoted images: {len(parent_media)} attached in chronological order]"
            )
        if not parent_text and not parent_media:
            parts.append("[Quoted message content unavailable]")
        parts.append("[End quoted message]")
        return {
            "text": "\n".join(parts),
            "media": parent_media,
            "parent_id": parent_id,
            "image_count": len(parent_media),
        }

    @staticmethod
    def _bounded_record_window(
        items: list[Any],
        *,
        anchor_id: str = "",
        anchor_time_ms: int = 0,
        limit: int = QUOTE_CONTEXT_MAX_RECORD_MESSAGES,
    ) -> list[Any]:
        """Take a bounded window centered on the anchor message or timestamp."""
        if len(items) <= limit:
            return list(items)
        ordered = sorted(items, key=FeishuChannel._message_time_ms)
        anchor_index = next(
            (
                index
                for index, item in enumerate(ordered)
                if anchor_id and str(getattr(item, "message_id", "") or "") == anchor_id
            ),
            None,
        )
        if anchor_index is None:
            times = [FeishuChannel._message_time_ms(item) for item in ordered]
            anchor_index = bisect_left(times, anchor_time_ms) if anchor_time_ms else 0
        start = max(0, anchor_index - limit // 2)
        start = min(start, len(ordered) - limit)
        return ordered[start : start + limit]

    @staticmethod
    def _merge_forward_record_window(
        items: list[Any],
        limit: int = QUOTE_CONTEXT_MAX_RECORD_MESSAGES,
    ) -> tuple[list[Any], int]:
        """Keep the beginning and end of a long merged-forward record."""
        ordered = sorted(items, key=FeishuChannel._message_time_ms)
        if len(ordered) <= limit:
            return ordered, 0
        head_count = max(1, limit // 2)
        tail_count = max(1, limit - head_count)
        window = [*ordered[:head_count], *ordered[-tail_count:]]
        return window, len(ordered) - len(window)

    async def _expand_forward_items(
        self,
        items: list[Any],
        *,
        resource_message_id: str,
        chat_id: str,
        depth: int,
        seen_forward_ids: set[str],
        budget: dict[str, int],
        current_message_id: str,
    ) -> list[dict[str, Any]]:
        """Recursively flatten nested merge_forward records with shared budgets."""
        entries: list[dict[str, Any]] = []
        indent = "  " * max(0, depth - 1)
        for item in items:
            if budget["messages"] <= 0:
                budget["omitted"] += 1
                continue
            item_id = str(getattr(item, "message_id", "") or "")
            if not item_id or item_id == current_message_id:
                continue
            item_type = self._message_type(item)
            if item_type == "merge_forward":
                label = await self._sender_label(item, chat_id)
                if depth >= QUOTE_CONTEXT_MAX_FORWARD_DEPTH or item_id in seen_forward_ids:
                    entries.append(
                        {
                            "line": (
                                f"{indent}- [Nested merge_forward from {label} "
                                "omitted: depth limit reached]"
                            ),
                            "media": [],
                            "time": self._message_time_ms(item),
                        }
                    )
                    budget["messages"] -= 1
                    continue
                entries.append(
                    {
                        "line": (
                            f"{indent}- [Nested merge_forward from {label} at "
                            f"{self._message_time_label(item)}]"
                        ),
                        "media": [],
                        "time": self._message_time_ms(item),
                    }
                )
                budget["messages"] -= 1
                nested_items = await self._fetch_message_items(item_id)
                if not nested_items:
                    if budget["messages"] > 0:
                        entries.append(
                            {
                                "line": f"{indent}  - [Nested merge_forward unavailable]",
                                "media": [],
                                "time": self._message_time_ms(item),
                            }
                        )
                        budget["messages"] -= 1
                    else:
                        budget["omitted"] += 1
                    continue
                nested_children = [
                    child
                    for child in nested_items
                    if str(getattr(child, "message_id", "") or "")
                    not in {item_id, current_message_id}
                ]
                if not nested_children:
                    nested_children = nested_items
                remaining_messages = max(0, budget["messages"])
                if remaining_messages <= 0:
                    budget["omitted"] += len(nested_children)
                    continue
                nested_window, nested_omitted = self._merge_forward_record_window(
                    nested_children,
                    limit=remaining_messages,
                )
                budget["omitted"] += nested_omitted
                entries.extend(
                    await self._expand_forward_items(
                        nested_window,
                        resource_message_id=item_id,
                        chat_id=chat_id,
                        depth=depth + 1,
                        seen_forward_ids={*seen_forward_ids, item_id},
                        budget=budget,
                        current_message_id=current_message_id,
                    )
                )
                continue

            item_text, item_media, _ = await self._parse_message_content(
                item,
                item_type,
                item_id,
                raw_content=self._message_content(item),
                max_images=budget["images"],
                resource_message_id=resource_message_id,
            )
            item_text = item_text.strip()
            if item_text in {"[image]", "[file]", "[audio]", "[sticker]"}:
                item_text = ""
            if not item_text and not item_media:
                if budget["images"] <= 0 and item_type == "image":
                    item_text = "[image omitted: limit reached]"
                else:
                    continue
            if budget["messages"] <= 0:
                budget["omitted"] += 1
                continue
            label = await self._sender_label(item, chat_id)
            display = item_text or f"[{item_type} message]"
            entries.append(
                {
                    "line": (
                        f"{indent}- [{label} at {self._message_time_label(item)}] "
                        f"{display}"
                    ),
                    "media": list(item_media),
                    "time": self._message_time_ms(item),
                }
            )
            budget["messages"] -= 1
            budget["images"] = max(0, budget["images"] - len(item_media))
        return entries

    async def _build_quoted_context(
        self,
        message: Any,
        *,
        max_images: int | None = None,
    ) -> dict[str, Any] | None:
        """Read a quoted message, merged-forward record, thread, or media burst."""
        parent_id = str(getattr(message, "parent_id", "") or "")
        current_message_id = str(getattr(message, "message_id", "") or "")
        if not parent_id:
            return None
        items = await self._fetch_message_items(parent_id)
        if not items:
            return None
        parent = next(
            (item for item in items if str(getattr(item, "message_id", "") or "") == parent_id),
            items[0],
        )
        parent_type = self._message_type(parent)
        parent_sender_id = self._message_sender_id(parent)
        chat_id = str(getattr(parent, "chat_id", "") or getattr(message, "chat_id", ""))
        parent_time = self._message_time_ms(parent)
        configured_quote_limit = max(1, int(getattr(self.config, "quote_max_images", 10)))
        image_limit = (
            configured_quote_limit
            if max_images is None
            else min(configured_quote_limit, max(0, int(max_images)))
        )

        # merge_forward stores the conversation in the children returned by message.get.
        if parent_type == "merge_forward":
            record_items = [
                item
                for item in items
                if str(getattr(item, "message_id", "") or "") != parent_id
                and str(getattr(item, "message_id", "") or "") != current_message_id
            ]
            if not record_items:
                record_items = items
            record_items, omitted_record_count = self._merge_forward_record_window(
                record_items,
                limit=QUOTE_CONTEXT_MAX_RECORD_MESSAGES,
            )
            budget = {
                "messages": QUOTE_CONTEXT_MAX_RECORD_MESSAGES,
                "images": image_limit,
                "omitted": omitted_record_count,
            }
            entries = await self._expand_forward_items(
                record_items,
                resource_message_id=parent_id,
                chat_id=chat_id,
                depth=1,
                seen_forward_ids={parent_id},
                budget=budget,
                current_message_id=current_message_id,
            )
            record_lines = [entry["line"] for entry in entries]
            record_media: list[tuple[int, int, str]] = []
            for entry in entries:
                entry_time = int(entry.get("time") or 0)
                record_media.extend(
                    (entry_time, index, path)
                    for index, path in enumerate(entry.get("media") or [])
                )
            record_media.sort(key=lambda value: (value[0], value[1]))
            media_paths = [path for _, _, path in record_media[:image_limit]]
            header = f"[Quoted conversation record: {len(record_lines)} messages"
            if budget["omitted"]:
                header += f", {budget['omitted']} messages omitted"
            header += "]"
            parts = [header, *record_lines]
            if media_paths:
                parts.append(
                    f"[Conversation images: {len(media_paths)} attached in chronological order]"
                )
            if not record_lines and not media_paths:
                parts.append("[Quoted conversation content unavailable]")
            parts.append("[End quoted conversation record]")
            context_text = "\n".join(parts)
            if len(context_text) > QUOTE_CONTEXT_MAX_CHARS:
                context_text = (
                    context_text[:QUOTE_CONTEXT_MAX_CHARS]
                    + "\n...[quoted context truncated]"
                )
            return {
                "text": context_text,
                "media": media_paths,
                "parent_id": parent_id,
                "image_count": len(media_paths),
            }

        # Topic threads are read as a bounded whole; ordinary groups use the
        # quoted parent plus a same-sender contiguous media segment.
        thread_id = str(getattr(parent, "thread_id", "") or "")
        if thread_id:
            record_items = await self._list_thread_messages(thread_id)
            if record_items:
                record_items = [
                    item
                    for item in record_items
                    if str(getattr(item, "message_id", "") or "") != current_message_id
                ]
                total_thread_items = len(record_items)
                record_items = self._bounded_record_window(
                    record_items,
                    anchor_id=parent_id,
                    anchor_time_ms=parent_time,
                    limit=QUOTE_CONTEXT_MAX_RECORD_MESSAGES,
                )
                omitted_thread_count = total_thread_items - len(record_items)
                record_lines = []
                record_media = []
                remaining = image_limit
                for item in record_items:
                    item_text, item_media, _ = await self._parse_message_content(
                        item,
                        self._message_type(item),
                        str(getattr(item, "message_id", "") or ""),
                        raw_content=self._message_content(item),
                        max_images=remaining,
                    )
                    item_text = item_text.strip()
                    if item_text in {"[image]", "[file]", "[audio]", "[sticker]"}:
                        item_text = ""
                    if not item_text and not item_media:
                        if remaining <= 0 and self._message_type(item) == "image":
                            item_text = "[image omitted: limit reached]"
                        else:
                            continue
                    label = await self._sender_label(item, chat_id)
                    display = item_text or f"[{self._message_type(item)} message]"
                    record_lines.append(
                        f"- [{label} at {self._message_time_label(item)}] {display}"
                    )
                    item_time = self._message_time_ms(item)
                    record_media.extend(
                        (item_time, index, path) for index, path in enumerate(item_media)
                    )
                    remaining -= len(item_media)
                record_media.sort(key=lambda value: (value[0], value[1]))
                media_paths = [path for _, _, path in record_media[:image_limit]]
                header = f"[Quoted thread: {len(record_lines)} messages"
                if omitted_thread_count:
                    header += f", {omitted_thread_count} messages omitted"
                header += "]"
                parts = [header, *record_lines]
                if media_paths:
                    parts.append(
                        f"[Conversation images: {len(media_paths)} attached in chronological order]"
                    )
                if not record_lines and not media_paths:
                    parts.append("[Quoted thread content unavailable]")
                parts.append("[End quoted thread]")
                context_text = "\n".join(parts)
                if len(context_text) > QUOTE_CONTEXT_MAX_CHARS:
                    context_text = (
                        context_text[:QUOTE_CONTEXT_MAX_CHARS]
                        + "\n...[quoted context truncated]"
                    )
                return {
                    "text": context_text,
                    "media": media_paths,
                    "parent_id": parent_id,
                    "image_count": len(media_paths),
                }

        parent_text, parent_media, _ = await self._parse_message_content(
            parent,
            parent_type,
            parent_id,
            raw_content=self._message_content(parent),
            max_images=image_limit,
        )
        parent_text = parent_text.strip()
        if parent_text == "[image]":
            parent_text = ""

        media: list[tuple[int, int, str]] = [
            (parent_time, index, path) for index, path in enumerate(parent_media)
        ]
        captions: list[str] = []
        if parent_media:
            nearby = await self._list_messages_near(chat_id, parent_time)
            segment = self._contiguous_same_sender_segment(
                parent,
                nearby,
                exclude_ids={current_message_id} if current_message_id else None,
            )
            remaining = max(0, image_limit - len(media))
            image_candidates = [
                item
                for item in segment
                if str(getattr(item, "message_id", "") or "") != parent_id
                and self._message_type(item) in {"image", "post"}
            ]
            for candidate in image_candidates:
                if remaining <= 0:
                    break
                _, paths, _ = await self._parse_message_content(
                    candidate,
                    self._message_type(candidate),
                    str(getattr(candidate, "message_id", "") or ""),
                    raw_content=self._message_content(candidate),
                    max_images=remaining,
                )
                candidate_time = self._message_time_ms(candidate)
                media.extend((candidate_time, index, path) for index, path in enumerate(paths))
                remaining -= len(paths)

            text_candidates = [
                item
                for item in segment
                if str(getattr(item, "message_id", "") or "") != parent_id
                and self._message_type(item) in {"text", "post"}
            ]
            before = [
                item
                for item in text_candidates
                if self._message_time_ms(item) <= parent_time
            ]
            after = [item for item in text_candidates if self._message_time_ms(item) > parent_time]
            selected_text = []
            if before:
                selected_text.append(max(before, key=self._message_time_ms))
            if after:
                selected_text.append(min(after, key=self._message_time_ms))
            for candidate in selected_text:
                text, _, _ = await self._parse_message_content(
                    candidate,
                    self._message_type(candidate),
                    str(getattr(candidate, "message_id", "") or ""),
                    raw_content=self._message_content(candidate),
                    max_images=0,
                )
                text = text.strip()
                if text and text not in {"[image]", "[file]", "[audio]", "[sticker]"}:
                    captions.append(text)

        media.sort(key=lambda item: (item[0], item[1]))
        media_paths = [path for _, _, path in media[:image_limit]]
        sender_label = await self._sender_label(parent, chat_id)
        parts = [
            f"[Quoted message from {sender_label} at {self._message_time_label(parent)}]"
        ]
        if parent_text:
            parts.append(parent_text)
        if media_paths:
            parts.append(
                f"[Related images from the same sender: {len(media_paths)} "
                "attached in chronological order]"
            )
        if captions:
            parts.append("[Nearby same-sender text]")
            parts.extend(f"- {text}" for text in captions)
        if not media_paths and image_limit == 0 and parent_type in {"image", "post"}:
            parts.append("[Quoted images omitted: turn image limit reached]")
        if not parent_text and not media_paths and len(parts) == 1:
            parts.append("[Quoted message content unavailable]")
        parts.append("[End quoted message]")
        context_text = "\n".join(parts)
        if len(context_text) > QUOTE_CONTEXT_MAX_CHARS:
            context_text = (
                context_text[:QUOTE_CONTEXT_MAX_CHARS]
                + "\n...[quoted context truncated]"
            )
        return {
            "text": context_text,
            "media": media_paths,
            "parent_id": parent_id,
            "image_count": len(media_paths),
        }

    async def _parse_message_content(
        self,
        message: Any,
        msg_type: str,
        message_id: str,
        *,
        raw_content: str | None = None,
        max_images: int | None = None,
        resource_message_id: str | None = None,
    ) -> tuple[str, list[str], list[dict[str, Any]]]:
        """Parse message content and extract media files with stable source references."""
        content = ""
        media = []
        media_refs: list[dict[str, Any]] = []
        raw_content = (
            str(raw_content)
            if raw_content is not None
            else self._message_content(message)
        )

        if msg_type == "text":
            try:
                content = json.loads(raw_content).get("text", "")
            except (json.JSONDecodeError, AttributeError):
                content = raw_content
        elif msg_type in ("image", "post"):
            content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")
            text_content = ""
            image_keys = []

            try:
                msg_content = json.loads(raw_content)

                if msg_type == "image":
                    image_key = msg_content.get("image_key")
                    if image_key:
                        image_keys.append(image_key)
                elif msg_type == "post":
                    # Extract all images and text from post content
                    post_content = msg_content.get("content", [])
                    text_parts = []

                    for block in post_content:
                        for element in block:
                            tag = element.get("tag")
                            if tag == "img":
                                img_key = element.get("image_key")
                                if img_key:
                                    image_keys.append(img_key)
                            elif tag == "text":
                                text_parts.append(element.get("text", ""))

                    text_content = " ".join(text_parts).strip()
                    if text_content:
                        content = text_content

                if max_images is not None:
                    image_limit = max(0, int(max_images))
                    omitted_images = max(0, len(image_keys) - image_limit)
                    image_keys = image_keys[:image_limit]
                    if omitted_images:
                        content = (
                            f"{content}\n[Images omitted: {omitted_images}; "
                            f"limit {image_limit} reached]"
                        )

                # Download images in parallel
                if image_keys:
                    download_message_id = resource_message_id or message_id
                    download_tasks = [
                        self._download_and_save_image(img_key, download_message_id)
                        for img_key in image_keys
                    ]
                    results = await asyncio.gather(*download_tasks)
                    for index, (image_key, path) in enumerate(
                        zip(image_keys, results, strict=False)
                    ):
                        if path is None:
                            continue
                        media.append(path)
                        media_refs.append(
                            {
                                "source": "feishu_im",
                                "message_id": download_message_id,
                                "image_key": image_key,
                                "index": index,
                                "cache_path": path,
                            }
                        )

            except Exception as e:
                logger.warning(f"Failed to process {msg_type} message: {e}")
        elif msg_type == "interactive":
            content = raw_content
        else:
            content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

        return content, media, media_refs

    async def _check_should_process(
        self, chat_type: str, chat_id: str, message: Any, is_mentioned: bool
    ) -> bool:
        """Check if message should be processed based on group/thread rules."""
        if chat_type != "group":
            return True

        chat_mode = await self._get_chat_mode(chat_id)

        # 普通群和话题群都根据 thread_require_mention 判断
        if self.config.thread_require_mention:
            # 模式1：所有消息都需要@才处理（普通群和话题群）
            if not is_mentioned:
                return False
        else:
            # 模式2：话题群仅首条消息不需要@，后续回复需要@
            if chat_mode == "thread":
                is_topic_starter = message.root_id == message.message_id or not message.root_id
                config = load_config()
                if not is_topic_starter and not is_mentioned and config.mode != BotMode.DEBUG:
                    return False
            # 普通群不需要@，直接处理

        return True

    def _save_user_name_cache(self, open_id: str, name: str) -> None:
        if open_id in self._user_name_cache:
            self._user_name_cache.pop(open_id)
        elif len(self._user_name_cache) >= self._MAX_USER_CACHE_SIZE:
            self._user_name_cache.popitem(last=False)
        self._user_name_cache[open_id] = name

    def _get_cached_user_name(self, open_id: str) -> str | None:
        if open_id not in self._user_name_cache:
            return None
        name = self._user_name_cache.pop(open_id)
        self._user_name_cache[open_id] = name
        return name

    def _save_chat_member_cache(
        self, chat_id: str, members: dict[str, str], last_error_at: float = 0
    ) -> None:
        if chat_id in self._chat_member_cache:
            self._chat_member_cache.pop(chat_id)
        elif len(self._chat_member_cache) >= self._CHAT_MEMBER_CACHE_MAX_CHATS:
            self._chat_member_cache.popitem(last=False)

        ttl = (
            self._CHAT_MEMBER_FETCH_COOLDOWN_SEC
            if last_error_at
            else self._CHAT_MEMBER_CACHE_TTL_SEC
        )
        self._chat_member_cache[chat_id] = {
            "members": members,
            "expires_at": time.time() + ttl,
            "last_error_at": last_error_at,
        }

    async def _fetch_chat_members(self, chat_id: str) -> dict[str, str]:
        if not self._client or not GetChatMembersRequest:
            return {}

        members: dict[str, str] = {}
        page_token = ""

        for _ in range(self._CHAT_MEMBER_FETCH_MAX_PAGES):
            request_builder = (
                GetChatMembersRequest.builder()
                .chat_id(chat_id)
                .member_id_type("open_id")
                .page_size(self._CHAT_MEMBER_FETCH_PAGE_SIZE)
            )
            if page_token:
                request_builder = request_builder.page_token(page_token)
            request = request_builder.build()
            response = await self._client.im.v1.chat_members.aget(request)
            if not response.success():
                raise RuntimeError(
                    f"client.im.v1.chat_members.get failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}"
                )

            data = response.data
            items = getattr(data, "items", []) if data else []
            for item in items:
                member_id = getattr(item, "member_id", "")
                name = getattr(item, "name", "")
                if member_id and name:
                    members[member_id] = name

            has_more = bool(getattr(data, "has_more", False)) if data else False
            next_page_token = getattr(data, "page_token", "") if data else ""
            if not has_more or not next_page_token:
                break
            page_token = next_page_token

        return members

    async def _get_group_member_name(self, chat_id: str, open_id: str) -> str | None:
        now = time.time()
        entry = self._chat_member_cache.get(chat_id)

        if entry:
            self._chat_member_cache.move_to_end(chat_id)
            members = entry.get("members", {})
            if entry.get("expires_at", 0) > now:
                return members.get(open_id)
            if (
                now - float(entry.get("last_error_at", 0) or 0)
                < self._CHAT_MEMBER_FETCH_COOLDOWN_SEC
            ):
                return members.get(open_id)

        try:
            members = await self._fetch_chat_members(chat_id)
            self._save_chat_member_cache(chat_id, members)
            return members.get(open_id)
        except Exception as e:
            logger.warning(f"Failed to get chat members for {chat_id}: {e}")
            stale_members: dict[str, str] = {}
            if entry:
                stale_members = entry.get("members", {})
            self._save_chat_member_cache(chat_id, stale_members, last_error_at=now)
            return stale_members.get(open_id)

    async def _get_user_name(self, open_id: str, chat_id: str | None = None) -> str | None:
        """
        Get user name from Feishu API by open_id.
        Returns user name if found, None otherwise.
        Uses LRU cache to avoid memory issues.
        """
        cached_name = self._get_cached_user_name(open_id)
        if cached_name:
            return cached_name

        try:
            if GetUserRequest:
                user_request = (
                    GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
                )
                user_response = self._client.contact.v3.user.get(user_request)
                if user_response.success() and user_response.data and user_response.data.user:
                    name = user_response.data.user.name
                    if name:
                        self._save_user_name_cache(open_id, name)
                        return name
        except Exception as e:
            logger.warning(f"Failed to get user name for {open_id}: {e}")

        if chat_id:
            member_name = await self._get_group_member_name(chat_id, open_id)
            if member_name:
                self._save_user_name_cache(open_id, member_name)
                return member_name

        return None

    async def _on_message(self, data: "P2ImMessageReceiveV1") -> None:
        """Handle incoming message from Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender
            message_id = message.message_id

            # 1. 消息去重
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # 定期清理去重缓存（每100条清理一次，减少开销）
            if (
                len(self._processed_message_ids) % 100 == 0
                and len(self._processed_message_ids) > 1000
            ):
                while len(self._processed_message_ids) > 500:
                    self._processed_message_ids.popitem(last=False)

            # 2. 跳过机器人自身消息
            if sender.sender_type == "bot":
                return

            # 3. 基础信息提取
            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            if sender_id == "unknown":
                logger.warning(f"Received message from unknown sender: {message_id}")
                return

            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" or "group"
            msg_type = message.message_type
            turn_image_limit = max(1, int(getattr(self.config, "turn_max_images", 10)))

            # 4. 解析消息内容和媒体
            content, media, media_refs = await self._parse_message_content(
                message,
                msg_type,
                message_id,
                max_images=turn_image_limit,
            )
            if not content:
                return

            # 5. 检查是否被@
            is_mentioned = False
            if hasattr(message, "mentions") and message.mentions:
                for mention in message.mentions:
                    if self._is_bot_mention(mention):
                        is_mentioned = True
                        break

            # 6. 检查是否需要处理该消息
            should_process = await self._check_should_process(
                chat_type, chat_id, message, is_mentioned
            )

            # 7. 添加已读表情
            if should_process:
                config = load_config()
                if config.mode != BotMode.DEBUG:
                    await self._add_reaction(message_id, "MeMeMe")

            # 8. 处理@占位符：从 message.mentions 中直接获取 name 和 id
            mention_name_map = {}
            if hasattr(message, "mentions") and message.mentions:
                for idx, mention in enumerate(message.mentions):
                    placeholder = f"@_user_{idx + 1}"
                    if placeholder not in content:
                        continue
                    mention_name = getattr(mention, "name", "")
                    if self._is_bot_mention(mention):
                        content = content.replace(placeholder, "")
                        continue
                    if hasattr(mention, "id") and mention.id:
                        user_id = mention.id.open_id
                        if mention_name:
                            mention_name_map[user_id] = mention_name
                        content = content.replace(placeholder, f"@{user_id}")

            # 8.5 群聊场景：处理用户姓名
            user_name = ""
            if chat_type == "group":
                user_name = mention_name_map.get(sender_id, "")
                if not user_name:
                    user_name = await self._get_user_name(sender_id, chat_id=chat_id) or ""
                if user_name:
                    content = f"[{user_name}]: {content}"

                for user_id, name in mention_name_map.items():
                    if name and f"@{user_id}" in content:
                        content = content.replace(f"@{user_id}", f"@{name}")
                content = re.sub(r"\s{2,}", " ", content).strip()

            quoted_context = None
            quoted_media_paths: list[str] = []
            if should_process and getattr(message, "parent_id", None):
                current_media = list(media)
                quote_image_slots = max(0, turn_image_limit - len(current_media))
                quoted_context = await self._build_quoted_context(
                    message,
                    max_images=quote_image_slots,
                )
                if quoted_context:
                    content = f"{content}\n\n{quoted_context['text']}"
                    # Keep the direct message's own media ahead of quoted media
                    # when the global ten-image turn budget is applied later.
                    quoted_media_paths = list(quoted_context.get("media") or [])[
                        : max(0, turn_image_limit - len(current_media))
                    ]
                    media = [*current_media, *quoted_media_paths][:turn_image_limit]

            # 9. 构建会话ID（处理话题群）
            reply_to = chat_id if chat_type == "group" else sender_id
            final_chat_id = chat_id
            chat_mode = None

            if chat_type == "group":
                chat_mode = await self._get_chat_mode(chat_id)
                if chat_mode == "thread":
                    # 话题首条消息设置root_id
                    if not message.root_id:
                        message.root_id = message.message_id
                    final_chat_id = f"{reply_to}#{message.root_id}"

            topic_title = ""
            if chat_mode == "thread" and message.root_id == message_id and msg_type == "post":
                try:
                    topic_title = json.loads(message.content).get("title", "")
                except (json.JSONDecodeError, AttributeError):
                    pass

            # 10. 转发到消息总线
            logger.info(f"Received message from Feishu: {content}")
            await self._handle_message(
                sender_id=sender_id,
                sender_name=user_name,
                chat_id=final_chat_id,
                content=content,
                media=media if media else None,
                media_refs=media_refs if media_refs else None,
                need_reply=should_process,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "reply_to": reply_to,
                    "msg_type": msg_type,
                    "root_id": message.root_id,
                    "chat_mode": chat_mode,
                    "topic_title": topic_title if isinstance(topic_title, str) else "",
                    "sender_id": sender_id,
                    "parent_id": getattr(message, "parent_id", "") or "",
                    "quoted_message_id": getattr(message, "parent_id", "") or "",
                    "quoted_context_applied": quoted_context is not None,
                    "quoted_image_count": len(quoted_media_paths),
                    "turn_image_count": len(media),
                    "turn_image_limit": turn_image_limit,
                    "quoted_media_paths": quoted_media_paths,
                    "media_refs": media_refs,
                },
            )

        except Exception:
            logger.exception("Error processing Feishu message")

    async def _download_viking_image(self, uri: str, msg: OutboundMessage | None) -> bytes:
        """Read an image with the same sender scope used by OpenViking tools."""
        from openviking.utils.media_limits import MAX_INLINE_TOOL_RESULT_MEDIA_BYTES
        from vikingbot.openviking_mount.ov_server import VikingClient

        sender_id = msg.metadata.get("sender_id") if msg else None
        if not isinstance(sender_id, str) or not sender_id.strip():
            raise ValueError("OpenViking image delivery requires the original sender identity")

        config = self._bot_config or load_config()
        client = await VikingClient.create(
            workspace_name(msg.session_key, config.sandbox.mode, portable=False),
            actor_peer_id=sender_id,
            config=config,
        )
        try:
            stat = await client.stat(uri)
            size = stat.get("size")
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or not 0 < size <= MAX_INLINE_TOOL_RESULT_MEDIA_BYTES
            ):
                raise ValueError(
                    "OpenViking image size is unavailable or exceeds the delivery limit"
                )
            data = await client.download_bytes(uri)
            if len(data) > size or sniff_image_format(data) is None:
                raise ValueError("OpenViking image changed size or has an unsupported format")
            return data
        finally:
            await client.close()

    async def _extract_and_upload_images(
        self, content: str, msg: OutboundMessage | None = None
    ) -> tuple[str, list[dict]]:
        """Upload explicit images, preserving URI citations and Markdown examples."""
        # Viking URIs also appear in listings and citations: only image Markdown
        # requests delivery. Keep bare send:// support for the image generation tool.
        # Consume code spans/blocks first so syntax examples never send an image.
        path = r"(?:[^\s()<>]|\([^()\n]*\))"
        uri = rf"send://{path}+?\.(?:png|jpe?g|gif|bmp|webp)(?:[?#][^\s<>)]*)?"
        image_uri = rf"(?:<(?:send|viking)://[^<>\n]+>|(?:send|viking)://{path}+?)"
        pattern = re.compile(
            r"(?P<literal>^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[^\n]*(?:\n|$)"
            r"[\s\S]*?(?:^[ \t]{0,3}(?P=fence)[`~]*[ \t]*(?:\n|$)|\Z)"
            r"|(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`)"
            r"|\\.)"
            rf"|!\[[^\]]*\]\(\s*(?P<image>{image_uri})"
            r"(?:\s+[\"'][^\n]*?[\"'])?\s*\)"
            rf"|(?P<bare>{uri})(?=$|[\s<>`\[\](){{}},;.!?。，；！：])",
            re.IGNORECASE | re.MULTILINE,
        )
        images: list[dict] = []
        replacements: dict[str, str] = {}
        parts: list[str] = []
        last_end = 0
        for match in pattern.finditer(content):
            if match.group("literal") is not None:
                continue
            img_url = match.group("image") or match.group("bare")
            img_url = img_url.removeprefix("<").removesuffix(">")
            scheme, location = img_url.split("://", 1)
            img_url = f"{scheme.lower()}://{location}"
            if img_url not in replacements:
                try:
                    if img_url.lower().startswith("viking://"):
                        result = await asyncio.wait_for(
                            self._download_viking_image(img_url, msg), timeout=60.0
                        )
                    else:
                        is_content, result = await self._parse_data_uri(img_url)
                        if is_content or not isinstance(result, bytes):
                            raise ValueError("Image reference did not resolve to image bytes")
                    image_key = await self._upload_image_to_feishu(result)
                    images.append({"image_key": image_key})
                    replacements[img_url] = ""
                except Exception as exc:
                    logger.warning(f"Failed to send image {img_url[:100]}: {exc}")
                    replacements[img_url] = "[图片发送失败]"
            parts.append(content[last_end : match.start()])
            parts.append(replacements[img_url])
            last_end = match.end()
        parts.append(content[last_end:])
        return "".join(parts).strip(), images
