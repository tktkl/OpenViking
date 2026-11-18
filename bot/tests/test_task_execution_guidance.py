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


@pytest.mark.asyncio
async def test_group_prompt_prefers_quoted_task_over_newer_topics(tmp_path):
    builder = ContextBuilder(tmp_path, is_group_chat=True)
    prompt = await builder._build_user_memory(
        SessionKey(type="feishu", channel_id="cli", chat_id="oc"),
        current_message="[Eden]: 你查的怎么样了",
        sender_id="ou_eden",
        ov_tools_enable=False,
    )

    assert "quoted task as the primary anchor" in prompt
    assert "newer, unrelated group topic" in prompt
    assert "ask one clarifying question" in prompt
