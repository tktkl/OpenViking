"""Opt-in configuration, independent of the upstream channel schema."""

from pydantic import BaseModel, Field, model_validator


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
    cli: LarkCLIConfig = Field(default_factory=LarkCLIConfig)

    @model_validator(mode="after")
    def validate_enabled(self):
        if self.enabled and (not self.helpdesk_id.strip() or not self.helpdesk_token.strip()):
            raise ValueError("Enabled Feishu Helpdesk requires helpdesk_id and helpdesk_token")
        return self
