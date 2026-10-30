"""Event types for the message bus."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from vikingbot.config.schema import SessionKey


class OutboundEventType(str, Enum):
    """Type of outbound message/event."""

    RESPONSE = "response"  # Normal response message
    INTERRUPT = "interrupt"  # Turn was interrupted before producing a final response
    TOOL_CALL = "tool_call"  # Tool being called
    TOOL_RESULT = "tool_result"  # Result from tool execution
    REASONING = "reasoning"  # Reasoning content
    CONTENT_DELTA = "content_delta"  # Streaming response text delta
    REASONING_DELTA = "reasoning_delta"  # Streaming reasoning text delta
    ITERATION = "iteration"  # Iteration marker
    NO_REPLY = "no_reply"  # No reply required
    RESPONSE_COMPLETED = "response_completed"  # Analytics-only response fact
    FEEDBACK_SUBMITTED = "feedback_submitted"  # Analytics-only explicit feedback fact
    RESPONSE_OUTCOME_EVALUATED = (
        "response_outcome_evaluated"  # Analytics-only implicit outcome fact
    )


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    sender_id: str  # User identifier
    content: str  # Message text
    session_key: SessionKey
    actor_peer_id: str | None = None  # Authenticated OpenViking peer identity
    timestamp: datetime = field(default_factory=datetime.now)
    sender_name: str | None = None
    need_reply: bool = True
    media: list[str | dict[str, Any]] = field(default_factory=list)  # Paths or image_url parts
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    openviking_connection: dict[str, Any] | None = None  # Internal OpenViking identity
    # In-process runtime event, never reconstructed from channel metadata or user text.
    # Running it on the main queue serializes history updates with agent turns.
    internal_handler: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    # Follow-up events may deliberately need an agent turn, but still must not
    # steer/cancel a live user turn. This flag is set only by host extensions.
    internal: bool = False
    # Host-only guard for queued extension work. Channel/user metadata cannot
    # construct this callable; the extension revalidates it at execution time.
    before_process: Callable[[], Awaitable[bool]] | None = field(default=None, repr=False)


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    session_key: SessionKey
    content: str
    event_type: OutboundEventType = OutboundEventType.RESPONSE
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    response_id: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    time_cost: float = field(default_factory=float)
    iteration: int = field(default_factory=int)
    tools_used_names: list[str] = field(default_factory=list)
    # Optional receipt for durable extension outboxes. True means the channel
    # explicitly confirmed delivery; exceptions/False/legacy None are unconfirmed.
    on_delivery: Callable[[bool], Awaitable[None]] | None = field(default=None, repr=False)

    @property
    def channel(self) -> str:
        """Get channel key from session key."""
        return self.session_key.channel_key()

    @property
    def is_normal_message(self) -> bool:
        """Check if this is a normal response message."""
        return self.event_type == OutboundEventType.RESPONSE
