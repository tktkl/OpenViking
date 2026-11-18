import pytest

from vikingbot.agent.context import ContextBuilder, TASK_EXECUTION_GUIDANCE
from vikingbot.config.schema import SessionKey


@pytest.mark.asyncio
async def test_system_prompt_contains_execution_discipline(tmp_path):
    builder = ContextBuilder(tmp_path)
    prompt = await builder.build_system_prompt(
        SessionKey(type="feishu", channel_id="cli", chat_id="oc"),
        ov_tools_enable=False,
    )

    assert TASK_EXECUTION_GUIDANCE in prompt
    assert "retrieve it before asking" in prompt
