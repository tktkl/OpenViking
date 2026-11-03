"""Opt-in configuration, independent of the upstream channel schema."""

from string import Formatter

from pydantic import BaseModel, Field, field_validator, model_validator

WELCOME = "你好，请描述遇到的问题，并补充相关截图或报错信息。"


class HelpdeskMessagesConfig(BaseModel):
    """A reserved prefix distinguishes user-owned automation from manual conversation."""

    prefix: str = Field(default="【服务台自动消息】", min_length=1, max_length=100)
    welcome: str = Field(default=WELCOME, min_length=1, max_length=1000)
    second_reminder_suffix: str = Field(
        default="如仍未收到你的补充或确认，{minutes}分钟后将自动关闭本工单。",
        min_length=1,
        max_length=1000,
    )
    closed_resolved: str = Field(default="问题已解决，工单已关闭。", min_length=1, max_length=1000)
    closed_timeout: str = Field(
        default="因两次提醒后仍未收到业务补充或确认，工单已关闭。", min_length=1, max_length=1000
    )

    @field_validator("prefix", "welcome", "closed_resolved", "closed_timeout")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Helpdesk automatic message text cannot be blank")
        return value.strip()

    @field_validator("second_reminder_suffix")
    @classmethod
    def validate_suffix(cls, value):
        try:
            fields = [
                (field, spec, conversion)
                for _, field, spec, conversion in Formatter().parse(value)
                if field is not None
            ]
            if fields != [("minutes", "", None)]:
                raise ValueError()
            value.format(minutes=5)
        except (ValueError, KeyError, IndexError):
            raise ValueError("second_reminder_suffix must contain exactly one {minutes}") from None
        return value.strip()

    def render(self, text):
        return f"{self.prefix}\n{text}"


class LarkCLIConfig(BaseModel):
    executable: str = Field(default="lark-cli", min_length=1)
    profile: str = ""
    timeout_seconds: float = Field(default=30, gt=0)
    auth_check_interval_seconds: float = Field(default=60, gt=0)
    keepalive_interval_seconds: float = Field(default=1800, gt=0)


class FeishuHelpdeskConfig(BaseModel):
    enabled: bool = False
    helpdesk_id: str = ""
    helpdesk_token: str = Field(default="", repr=False)
    followup_interval_minutes: int = Field(default=5, gt=0, strict=True)
    messages: HelpdeskMessagesConfig = Field(default_factory=HelpdeskMessagesConfig)
    cli: LarkCLIConfig = Field(default_factory=LarkCLIConfig)

    @model_validator(mode="after")
    def validate_enabled(self):
        if self.enabled and (not self.helpdesk_id.strip() or not self.helpdesk_token.strip()):
            raise ValueError("Enabled Feishu Helpdesk requires helpdesk_id and helpdesk_token")
        return self
