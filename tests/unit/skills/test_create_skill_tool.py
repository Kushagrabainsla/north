"""Tests for the create_skill tool."""

from __future__ import annotations

from skills.registry import SkillRegistry
from tools.models import ToolInput
from tools.universal.create_skill import NEAR_COPY_SIMILARITY, CreateSkillTool


class _Selector:
    """Scores two skills by the share of description words they have in common."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def closest(self, skill, others):
        if self.fail:
            raise RuntimeError("no embedding model")

        def words(item) -> set[str]:
            return set(item.description.lower().rstrip(".").split())

        scored = [(len(words(skill) & words(o)) / len(words(skill) | words(o)), o) for o in others]
        return max(scored, key=lambda pair: pair[0], default=None)


def _tested(name: str, **params) -> dict:
    return {"name": name, **params}


def test_authoring_schema_exposes_exact_contract_fields_and_fixed_intents():
    properties = CreateSkillTool.parameters_schema["properties"]
    contract = properties["execution"]
    assert set(contract["required"]) == {"tools", "approval", "inputs", "outputs", "success_criteria"}
    # North runs every skill: there is no agent for a contract to name.
    assert "agent" not in contract["properties"]
    assert contract["additionalProperties"] is False
    assert "review" in properties["intents"]["items"]["enum"]
    # A task picks a skill by its description; there are no example requests to test it with.
    assert "positive_prompts" not in properties and "negative_prompts" not in properties


async def test_creates_and_reloads_skill(tmp_path):
    builtin_dir = tmp_path / "builtin"
    learned_dir = tmp_path / "learned"
    builtin_dir.mkdir()
    learned_dir.mkdir()

    registry = SkillRegistry(builtin_dir=builtin_dir, learned_dir=learned_dir)
    tool = CreateSkillTool(registry=registry, learned_dir=learned_dir, skill_selector=_Selector())

    out = await tool.run(
        ToolInput(
            params=_tested(
                "deploy-docker",
                description="Use when deploying a Docker container.",
                instructions="Step 1: docker build\nStep 2: docker run",
            )
        )
    )

    assert out.success, out.error
    assert out.data["name"] == "deploy-docker"

    # Created on disk, checked, and live in one call
    skill = registry.get("deploy-docker")
    assert skill.description == "Use when deploying a Docker container."
    assert "docker build" in skill.body
    assert skill.status == "active"
    assert skill.domains == frozenset({"general"})
    assert skill.available_to("general") is True


async def test_missing_params_errors(tmp_path):
    registry = SkillRegistry(builtin_dir=tmp_path)
    tool = CreateSkillTool(registry=registry, learned_dir=tmp_path)

    out = await tool.run(ToolInput(params={"name": "test"}))
    assert not out.success
    assert "description" in out.error


async def test_user_skill_update_requires_fresh_approval_and_does_not_grant_ownership(tmp_path):
    from config.approval_mode import ApprovalMode
    from policies.self_edit import SelfEditPolicy
    from tests.conftest import approving_store, bind_approvals, rejecting_store

    learned = tmp_path / "learned"
    registry = SkillRegistry(tmp_path / "builtin", learned)
    tool = CreateSkillTool(registry, learned, skill_selector=_Selector())
    created = await tool.run(
        ToolInput(
            params=_tested(
                "mine",
                description="Use when reviewing my documents.",
                instructions="1. Read the input.\n2. Return a review.",
            )
        )
    )
    assert created.success
    path = learned / "mine" / "SKILL.md"
    original = path.read_text()
    policy = SelfEditPolicy(learned, tmp_path / "mutations", user_document="SKILL.md")
    tool._self_edit_policy = policy
    call = ToolInput(
        params=_tested(
            "mine",
            action="update",
            description="Use when reviewing my documents.",
            instructions="1. Read the input.\n2. Return a concise review.",
        )
    )
    assert not (await tool.run(call)).success
    assert (await tool.describe(call)).requires_confirmation
    rejected = rejecting_store()
    bind_approvals(tool, ApprovalMode.YOLO, store=rejected)
    assert not (await tool.execute(call)).success
    rejected.wait_for_decision.assert_awaited_once()
    assert path.read_text() == original
    approved = approving_store()
    bind_approvals(tool, ApprovalMode.YOLO, store=approved)
    assert (await tool.execute(call)).success
    approved.wait_for_decision.assert_awaited_once()
    assert policy.authorize(path, "update")
    assert policy.revert(policy._read_registry()[-1]["id"])
    assert path.read_text() == original


async def test_a_near_copy_of_a_live_skill_is_refused_and_nothing_is_written(tmp_path):
    """Two skills that say the same thing split a task's choice between them."""
    learned = tmp_path / "learned"
    registry = SkillRegistry(tmp_path / "builtin", learned)
    tool = CreateSkillTool(registry, learned, skill_selector=_Selector())
    body = "1. Build the image.\n2. Run the container.\n\n## Done when\nThe service is healthy."
    assert (
        await tool.run(
            ToolInput(
                params=_tested("deploy-docker", description="Use when deploying a Docker container.", instructions=body)
            )
        )
    ).success

    copy = await tool.run(
        ToolInput(
            params=_tested("ship-docker", description="Use when deploying a Docker container.", instructions=body)
        )
    )
    other = await tool.run(
        ToolInput(
            params=_tested(
                "weekly-status", description="Use when writing my weekly status report.", instructions="Wins."
            )
        )
    )

    assert not copy.success and "near-copy of the skill 'deploy-docker'" in copy.error
    assert "ship-docker" not in registry.names() and not (learned / "ship-docker").exists()
    assert other.success, other.error


async def test_an_update_is_not_compared_with_the_skill_it_replaces(tmp_path):
    learned = tmp_path / "learned"
    registry = SkillRegistry(tmp_path / "builtin", learned)
    tool = CreateSkillTool(registry, learned, skill_selector=_Selector())
    base = {"description": "Use when deploying a Docker container.", "instructions": "1. Build.\n2. Run."}
    assert (await tool.run(ToolInput(params=_tested("deploy-docker", **base)))).success

    edit = await tool.run(
        ToolInput(params=_tested("deploy-docker", action="update", **{**base, "instructions": "1. Build.\n2. Push."}))
    )

    assert edit.success, edit.error
    assert "Push" in registry.get("deploy-docker").body


async def test_the_near_copy_check_is_skipped_when_embeddings_fail(tmp_path):
    learned = tmp_path / "learned"
    registry = SkillRegistry(tmp_path / "builtin", learned)
    tool = CreateSkillTool(registry, learned, skill_selector=_Selector(fail=True))
    base = {"description": "Use when deploying a Docker container.", "instructions": "1. Build.\n2. Run."}

    assert (await tool.run(ToolInput(params=_tested("deploy-docker", **base)))).success
    assert (await tool.run(ToolInput(params=_tested("ship-docker", **base)))).success


def test_the_near_copy_bar_sits_between_different_skills_and_rewordings():
    """0.838 is the closest pair of different general skills; 0.899 the least similar rewording of one."""
    assert 0.838 < NEAR_COPY_SIMILARITY < 0.899


async def test_an_edit_that_fails_its_checks_leaves_the_live_skill_as_it_was(tmp_path):
    """An edit used to take the skill offline at once: the Oct 7 research briefing stopped that way."""
    learned = tmp_path / "learned"
    registry = SkillRegistry(tmp_path / "builtin", learned)
    tool = CreateSkillTool(registry, learned, skill_selector=_Selector())
    base = {"description": "Use when deploying a Docker container.", "instructions": "1. Build.\n2. Run."}
    assert (await tool.run(ToolInput(params=_tested("deploy-docker", **base)))).success
    before = (learned / "deploy-docker" / "SKILL.md").read_text()

    edit = await tool.run(
        ToolInput(
            params=_tested(
                "deploy-docker", action="update", description="Deploys anything.", instructions="Do whatever."
            )
        )
    )

    assert not edit.success and "Use when" in edit.error
    assert (learned / "deploy-docker" / "SKILL.md").read_text() == before
    assert registry.get("deploy-docker").status == "active"


async def test_creates_skill_with_flow_execution_contract(tmp_path):
    registry = SkillRegistry(tmp_path / "builtin", tmp_path / "learned")
    tool = CreateSkillTool(registry, tmp_path / "learned", skill_selector=_Selector())

    out = await tool.run(
        ToolInput(
            params={
                **_tested("review-item"),
                "description": "Use when reviewing one item.",
                "instructions": "1. Review the item.\n2. Return evidence.",
                "execution": {
                    "agent": "general",
                    "tools": [],
                    "approval": "never",
                    "inputs": {
                        "type": "object",
                        "properties": {"item": {"type": "string"}},
                        "required": ["item"],
                    },
                    "outputs": {
                        "type": "object",
                        "properties": {"result": {"type": "string"}},
                        "required": ["result"],
                    },
                    "success_criteria": ["The result cites the supplied item."],
                },
            }
        )
    )

    assert out.success
    execution = registry.get("review-item").execution
    assert execution is not None
    assert execution.agent == "general"
    assert execution.inputs["required"] == ["item"]


async def test_a_skill_you_edited_by_hand_can_be_checked_and_switched_on(tmp_path):
    """With the self-edit policy on, as the server runs it.

    A skill edited on the dashboard is a candidate North never authored. Activation went through the
    policy's "update", which refuses files North did not create, so it could never be activated again.
    """
    from policies.self_edit import SelfEditPolicy

    builtin, learned = tmp_path / "builtin", tmp_path / "learned"
    builtin.mkdir()
    (learned / "deploy-docker").mkdir(parents=True)
    (learned / "deploy-docker" / "SKILL.md").write_text(
        "---\nname: deploy-docker\ndescription: Use when deploying a Docker container.\nsource: learned\n"
        "status: candidate\ndomains:\n- general\n---\n\n1. Build the image.\n2. Run the container.\n\n"
        "## Done when\nThe service is healthy.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(builtin, learned)
    policy = SelfEditPolicy(learned, tmp_path / "mutations")
    tool = CreateSkillTool(registry, learned, skill_selector=_Selector(), self_edit_policy=policy)

    activated = await tool.run(ToolInput(params=_tested("deploy-docker", action="activate")))
    rewritten = await tool.run(
        ToolInput(
            params=_tested(
                "deploy-docker",
                action="update",
                description="Use when deploying anything.",
                instructions="Do whatever.\n\n## Done when\nDone.",
            )
        )
    )

    assert activated.success, activated.error
    assert registry.get("deploy-docker").status == "active"
    assert not rewritten.success, "North still may not rewrite what you wrote"


async def test_no_card_for_a_call_that_will_be_refused_anyway(tmp_path):
    """Four refused updates to a skill North did not write cost the user four approvals."""
    from policies.self_edit import SelfEditPolicy

    builtin, learned = tmp_path / "builtin", tmp_path / "learned"
    builtin.mkdir()
    (learned / "mine").mkdir(parents=True)
    (learned / "mine" / "SKILL.md").write_text(
        "---\nname: mine\ndescription: Use when doing my thing.\nsource: learned\nstatus: candidate\n"
        "domains:\n- general\n---\n\nDo it.\n\n## Done when\nDone.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry(builtin, learned)
    tool = CreateSkillTool(
        registry, learned, skill_selector=_Selector(), self_edit_policy=SelfEditPolicy(learned, tmp_path / "m")
    )
    update = {"action": "update", "name": "mine", "description": "Use when x.", "instructions": "y"}

    assert await tool.describe(ToolInput(params=update)) is None
    assert await tool.describe(ToolInput(params={"action": "activate", "name": "nobody"})) is None
    assert not (await tool.execute(ToolInput(params=update))).success
    new = {"action": "create", "name": "fresh", "description": "Use when fresh.", "instructions": "z"}
    assert await tool.describe(ToolInput(params=new)) is not None, "a write that can happen is still asked about"
