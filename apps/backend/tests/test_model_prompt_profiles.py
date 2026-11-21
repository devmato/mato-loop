import pytest

from mato_loop_backend.agent.model_prompt_profiles import build_model_instructions
from mato_loop_backend.agent.prompts import agent_profile_runtime_instructions
from mato_loop_backend.settings import (
    AgentProfileOverrides,
    CustomAgentProfile,
    build_profile,
    general_profile,
    interactive_profile,
    plan_profile,
    resolve_agent_profile,
)


@pytest.mark.parametrize(
    "model_id",
    [
        "Qwen3.8-27B",
        "Qwen/Qwen3.8-27B",
        "qwen3.8_27b",
        "Qwen3.8-27B-GGUF-UD-Q4_K_XL",
        "Qwen3.8-27B:latest",
    ],
)
def test_base_model_and_named_variants_share_one_instruction(model_id):
    instruction = build_model_instructions("Qwen3.8-27B")

    assert instruction and build_model_instructions(model_id) == instruction


@pytest.mark.parametrize(
    "profile,delegated,expected",
    [
        (build_profile(), False, True),
        (build_profile(), True, False),
        (interactive_profile(), False, True),
        (interactive_profile(), True, False),
        (plan_profile(), False, False),
        (general_profile(), False, False),
        (None, False, False),
    ],
)
def test_prompt_scope_and_model_switch(profile, delegated, expected):
    prompt = agent_profile_runtime_instructions(
        profile,
        model_id="Qwen3.8-27B",
        delegated=delegated,
    )

    assert (build_model_instructions("Qwen3.8-27B") in prompt) is expected


@pytest.mark.parametrize("profile", [plan_profile(), build_profile(), interactive_profile()])
def test_coding_profiles_share_batching_guidance(profile):
    prompt = agent_profile_runtime_instructions(profile)
    assert "batch independent read, glob, and grep calls" in prompt
    assert "group coherent changes to the same file into one patch" in prompt


@pytest.mark.parametrize("profile", [plan_profile(), build_profile(), interactive_profile()])
def test_coding_profiles_require_source_checks_for_specific_artifacts(profile):
    assert (
        "When an answer depends on a specific implementation or work artifact, check the relevant source before "
        "answering. If you can’t verify it, say so."
    ) in agent_profile_runtime_instructions(profile)


@pytest.mark.parametrize("profile", [plan_profile(), build_profile(), interactive_profile()])
def test_coding_memory_restriction_reaches_delegated_agents(profile):
    prompt = agent_profile_runtime_instructions(profile, delegated=True)
    assert all(rule in prompt for rule in (
        "Retrieve saved memories or conversations outside the current project only when the user explicitly",
        "context_search with scope=sessions and all_projects=false",
        "Do not use scope=all for project context",
    ))
    if profile.id == interactive_profile().id:
        assert "Only an explicit implementation request authorizes changes" in prompt


def test_custom_profile_inherits_build_model_instructions():
    profile = resolve_agent_profile(
        CustomAgentProfile(
            name="Custom Build",
            description="",
            base_profile_id=build_profile().id,
            overrides=AgentProfileOverrides(),
        ),
        build_profile(),
        None,
    )

    prompt = agent_profile_runtime_instructions(
        profile,
        model_id="Qwen3.8-27B",
    )

    assert build_model_instructions("Qwen3.8-27B") in prompt
