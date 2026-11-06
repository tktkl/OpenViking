"""Opt-in configuration, independent of the upstream channel schema."""

from string import Formatter
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from vikingbot.extensions.feishu_helpdesk.escalation import normalize_skill_group, normalize_system

WELCOME = "您好，请描述遇到的问题，并补充相关截图或其他信息。如已经提供，可忽略此消息。"


class HelpdeskMessagesConfig(BaseModel):
    """A reserved prefix distinguishes user-owned automation from manual conversation."""

    prefix: str = Field(default="【AI分身自动回复】", min_length=1, max_length=100)
    welcome: str = Field(default=WELCOME, min_length=1, max_length=1000)
    second_reminder_suffix: str = Field(
        default="如仍未收到{audience}，{minutes}分钟后将自动关闭本工单。",
        min_length=1,
        max_length=1000,
    )
    escalation_reminder_suffix: str = Field(
        default="如仍未收到{audience}，{minutes}分钟后将升级处理。",
        min_length=1,
        max_length=1000,
    )
    closed_resolved: str = Field(default="问题已解决，工单已关闭。", min_length=1, max_length=1000)
    closed_timeout: str = Field(
        default="因催办后仍未收到业务补充或确认，工单已关闭。", min_length=1, max_length=1000
    )

    @field_validator("prefix", "welcome", "closed_resolved", "closed_timeout")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Helpdesk automatic message text cannot be blank")
        return value.strip()

    @field_validator("second_reminder_suffix", "escalation_reminder_suffix")
    @classmethod
    def validate_suffix(cls, value):
        try:
            fields = [
                (field, spec, conversion)
                for _, field, spec, conversion in Formatter().parse(value)
                if field is not None
            ]
            names = [field for field, _, _ in fields]
            if (
                names.count("minutes") != 1
                or names.count("audience") > 1
                or any(field not in {"minutes", "audience"} for field in names)
                or any(spec or conversion for _, spec, conversion in fields)
            ):
                raise ValueError()
            value.format(minutes=5, audience="相关方")
        except (ValueError, KeyError, IndexError):
            raise ValueError(
                "reminder suffix must contain one {minutes} and optional {audience}"
            ) from None
        return value.strip()

    def render(self, text):
        return f"{self.prefix}\n{text}"


class LarkCLIConfig(BaseModel):
    executable: str = Field(default="lark-cli", min_length=1)
    profile: str = ""
    timeout_seconds: float = Field(default=30, gt=0)
    auth_mcp_server: str = Field(default="wangoon-auth-mcp", min_length=1)
    provider: str = Field(default="lark-cli", min_length=1)
    environment: str = Field(default="prod", min_length=1)
    auth_profile: str = Field(default="default", min_length=1)
    token_cache_seconds: float = Field(default=60, ge=0)


class FeishuHelpdeskReplyConfig(BaseModel):
    """Reply transport; lark_cli stays the backward-compatible default."""

    mode: Literal["lark_cli", "feishu_web"] = "lark_cli"
    auth_mcp_server: str = "wangoon-auth-mcp"
    provider: str = "lark-qr"
    environment: str = "prod"
    profile: str = "default"
    actor_open_id: str = ""
    gateway_url: str = "https://internal-api-lark-api.feishu.cn/im/gateway/"
    web_url: str = ""
    feed_ids: dict[str, str] = Field(default_factory=dict)
    auto_resolve_feed_id: bool = False
    browser_executable: str = ""
    feed_resolution_timeout_seconds: float = Field(default=30, gt=0)
    feed_cache_path: str = ""
    cookie_cache_seconds: float = Field(default=60, ge=0)

    @model_validator(mode="after")
    def validate_web_reply(self):
        if self.mode == "feishu_web":
            if not self.actor_open_id.strip():
                raise ValueError("feishu_web reply requires actor_open_id")
            if not self.auth_mcp_server.strip():
                raise ValueError("feishu_web reply requires auth_mcp_server")
        return self


class FeishuHelpdeskEscalationConfig(BaseModel):
    """Configurable system-to-group routing for unresolved escalations."""

    enabled: bool = True
    interval_minutes: int = Field(default=45, gt=0, strict=True)
    sender: Literal["bot", "lark_cli"] = "bot"
    groups: dict[str, str] = Field(default_factory=dict)
    followup_skill_groups: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("groups")
    @classmethod
    def valid_groups(cls, value):
        groups = {}
        for system, chat_id in value.items():
            name, target = normalize_system(system), str(chat_id).strip()
            if not name or not target.startswith("oc_"):
                raise ValueError("escalation groups require system names and oc_ chat ids")
            if name in groups:
                raise ValueError("escalation group system names are case-insensitive duplicates")
            groups[name] = target
        return groups

    @model_validator(mode="after")
    def validate_followup_skill_groups(self):
        scope = {}
        seen = set()
        for group, systems in self.followup_skill_groups.items():
            label = str(group).strip()
            name = normalize_skill_group(label)
            if not name:
                raise ValueError("followup skill groups require non-empty names")
            if name in seen:
                raise ValueError("followup skill groups are case-insensitive duplicates")
            seen.add(name)
            if not isinstance(systems, list) or not systems:
                raise ValueError("followup skill groups require a non-empty system list")
            normalized_systems = []
            for system in systems:
                system_name = normalize_system(system)
                if system_name not in self.groups:
                    raise ValueError(
                        f"followup skill group {group!r} references unconfigured system {system!r}"
                    )
                if system_name not in normalized_systems:
                    normalized_systems.append(system_name)
            scope[label] = normalized_systems
        self.followup_skill_groups = scope
        return self


class FeishuHelpdeskConfig(BaseModel):
    enabled: bool = False
    helpdesk_id: str = ""
    helpdesk_token: str = Field(default="", repr=False)
    followup_interval_minutes: int = Field(default=5, gt=0, strict=True)
    messages: HelpdeskMessagesConfig = Field(default_factory=HelpdeskMessagesConfig)
    cli: LarkCLIConfig = Field(default_factory=LarkCLIConfig)
    reply: FeishuHelpdeskReplyConfig = Field(default_factory=FeishuHelpdeskReplyConfig)
    escalation: FeishuHelpdeskEscalationConfig = Field(
        default_factory=FeishuHelpdeskEscalationConfig
    )

    @model_validator(mode="after")
    def validate_enabled(self):
        if self.enabled and (not self.helpdesk_id.strip() or not self.helpdesk_token.strip()):
            raise ValueError("Enabled Feishu Helpdesk requires helpdesk_id and helpdesk_token")
        return self
