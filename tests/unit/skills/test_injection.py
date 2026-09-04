"""Tests for skill injection into an engineering agent's context."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agents.base import Agent
from agents.models import AgentDependencies, AgentPayload
from skills.registry import SkillRegistry
from skills.selector import SkillSelector

_VOCAB = ("migration", "tool")


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for text in texts:
        lowered = text.lower()
        vector = [1.0 if word in lowered else 0.0 for word in _VOCAB]
        # An orthogonal "no-match" dimension so unrelated text is dissimilar to
        # every skill rather than accidentally parallel to one.
        vector.append(0.0 if any(vector) else 1.0)
        vectors.append(vector)
    return vectors


class _StubAgent(Agent):
    async def _execute(self, payload, context, scored_tools):
        return {}


async def _skills_block(agent: Agent, prompt: str) -> str:
    """Select then render, the way `Agent.run()` does it.

    Selection is a separate step so a run embeds the prompt once and shares the
    result with tool ranking; these tests exercise both halves together.
    """
    return await agent._load_skills_block(
        AgentPayload(task_id="t", prompt=prompt),
        await agent._select_skills(prompt),
    )


def _write_skill(base: Path, name: str, description: str, domains: list[str] | None = None) -> None:
    directory = base / name
    directory.mkdir(parents=True)
    domains_line = f"domains: [{', '.join(domains)}]\n" if domains else ""
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{domains_line}---\nBODY-{name}", encoding="utf-8"
    )


def _agent(registry: SkillRegistry, selector: SkillSelector) -> _StubAgent:
    deps = AgentDependencies(
        context_store=None,
        inference_router=None,
        tool_registry=None,
        confidence_tracker=None,
        stream_manager=None,
        skill_registry=registry,
        skill_selector=selector,
    )
    return _StubAgent(SimpleNamespace(agent="coder", domain="engineering"), deps)


async def test_offers_the_selected_skill_by_description_not_body(tmp_path):
    """The body is fetched with use_skill, never pasted into the prompt.

    The opening block of an agent conversation is re-sent on every turn, so a
    pasted playbook is paid ~20 times per task whether the model uses it or not.
    """
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    _write_skill(tmp_path, "add-tool", "Use when adding a tool")
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed, min_similarity=0.5)
    agent = _agent(registry, selector)

    block = await _skills_block(agent, "add a database migration")

    assert "db-migration" in block  # the match is offered by name
    assert "Use when adding a database migration" in block  # ...and by its description
    assert "BODY-db-migration" not in block  # but the procedure itself is not pasted in
    assert "use_skill" in block  # the model is told how to fetch it
    assert "advisory" in block.lower()  # framed as advisory, not authoritative
    assert "add-tool" in block  # non-selected skills stay discoverable by name


async def test_no_skills_returns_empty(tmp_path):
    registry = SkillRegistry(builtin_dir=tmp_path)  # empty dir
    selector = SkillSelector(registry, embed_fn=_fake_embed)
    agent = _agent(registry, selector)
    assert await _skills_block(agent, "anything") == ""


async def test_nothing_selected_still_lists_available(tmp_path):
    _write_skill(tmp_path, "add-tool", "Use when adding a tool")
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed, min_similarity=0.5)
    agent = _agent(registry, selector)

    block = await _skills_block(agent, "totally unrelated banana")

    assert "Other skills available" in block
    assert "add-tool" in block


class _FakeMemory:
    """Minimal memory gateway so _load_context runs past its recall step in tests."""

    async def principal_for(self, name, domain):
        return object()

    async def recall(self, principal, prompt):
        return SimpleNamespace(render=lambda: "")


def _agent_for_domain(domain: str, registry: SkillRegistry, selector: SkillSelector) -> _StubAgent:
    deps = AgentDependencies(
        context_store=None,
        inference_router=None,
        tool_registry=None,
        confidence_tracker=None,
        stream_manager=None,
        skill_registry=registry,
        skill_selector=selector,
        memory=_FakeMemory(),
    )
    return _StubAgent(SimpleNamespace(agent=domain, domain=domain), deps)


def test_skills_enabled_domains_covers_engineering_and_general():
    from agents.base import _SKILLS_ENABLED_DOMAINS

    # Engineering agents and the general assistant receive skills. General is enabled
    # for the research/literature-review skill (knowledge-synthesis routes to general);
    # other domains stay off until they have skills of their own - see the constant's note.
    assert "engineering" in _SKILLS_ENABLED_DOMAINS
    assert "general" in _SKILLS_ENABLED_DOMAINS
    assert "finance" not in _SKILLS_ENABLED_DOMAINS


def test_skill_domains_default_to_engineering_and_parse_from_frontmatter(tmp_path):
    _write_skill(tmp_path, "eng-only", "Use when doing engineering")
    _write_skill(tmp_path, "cross", "Use when doing cross-domain work", domains=["engineering", "general"])
    by_name = {s.name: s for s in SkillRegistry(builtin_dir=tmp_path).all()}
    assert by_name["eng-only"].domains == frozenset({"engineering"})  # default
    assert by_name["eng-only"].available_to("engineering") and not by_name["eng-only"].available_to("general")
    assert by_name["cross"].available_to("engineering") and by_name["cross"].available_to("general")


async def test_load_skills_block_filters_by_agent_domain(tmp_path):
    # The domain-eligibility mechanism (used once non-engineering domains are enabled):
    # _load_skills_block only considers skills whose domains include the agent's, so an
    # engineering-only skill is never even a candidate for a general agent, and a
    # general-eligible skill is.
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")  # engineering (default)
    _write_skill(tmp_path, "add-tool", "Use when adding a tool", domains=["general"])  # general-eligible
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed, min_similarity=0.4)
    general = _agent_for_domain("general", registry, selector)

    migration_block = await _skills_block(general, "add a database migration")
    assert "db-migration" not in migration_block  # engineering-only: not eligible, not even listed

    tool_block = await _skills_block(general, "add a tool")
    assert "add-tool" in tool_block  # general-eligible skill is offered
    assert "Use when adding a tool" in tool_block


async def test_engineering_agent_still_gets_engineering_skills(tmp_path):
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed, min_similarity=0.4)
    ctx = await _agent_for_domain("engineering", registry, selector)._load_context(
        AgentPayload(task_id="t", prompt="add a database migration")
    )
    assert "db-migration" in ctx
    assert "Use when adding a database migration" in ctx


async def test_three_skills_are_offered_not_two(tmp_path):
    """Descriptions are cheap enough to offer a real choice.

    A third candidate costs ~50 tokens where a third body would have cost ~800,
    so the model gets an alternative when the top match is not quite right.
    """
    for name in ("db-migration", "db-schema", "db-index", "unrelated-topic"):
        _write_skill(tmp_path, name, f"Use when working on {name.replace('-', ' ')}")
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed, min_similarity=0.0)

    selected = await selector.select("db migration schema index")
    assert len(selected) == 3


async def test_no_skill_body_ever_reaches_the_prompt(tmp_path):
    """The saving only holds if this is true for every skill, selected or not."""
    for name in ("alpha", "beta", "gamma", "delta"):
        _write_skill(tmp_path, name, f"Use when doing {name}")
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed, min_similarity=0.0)
    agent = _agent(registry, selector)

    block = await _skills_block(agent, "doing alpha")

    assert block  # something was offered
    assert not any(f"BODY-{name}" in block for name in ("alpha", "beta", "gamma", "delta"))
