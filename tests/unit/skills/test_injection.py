"""Tests for the skills an agent's context lists, and the flow skills it gives in full."""

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
    """Select then render, the way `Agent.run()` does it."""
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


class _FakeMemory:
    """Minimal memory gateway so _load_context runs past its recall step in tests."""

    async def principal_for(self, name, domain):
        return object()

    async def recall(self, principal, prompt):
        return SimpleNamespace(render=lambda: "")


class _RunStore:
    def __init__(self) -> None:
        self.skills: list[list[dict]] = []

    async def set_skills(self, run_id, skills):
        self.skills.append(skills)


def _agent(registry: SkillRegistry, domain: str = "engineering", **deps) -> _StubAgent:
    dependencies = AgentDependencies(
        context_store=None,
        inference_router=None,
        tool_registry=None,
        confidence_tracker=None,
        stream_manager=None,
        skill_registry=registry,
        memory=_FakeMemory(),
        **deps,
    )
    return _StubAgent(SimpleNamespace(agent=domain, domain=domain), dependencies)


async def test_every_skill_is_listed_by_name_and_description_not_body(tmp_path):
    """The task model chooses its skill from the list; the body is fetched with use_skill.

    The opening block of an agent conversation is re-sent on every turn, so a
    pasted playbook is paid ~20 times per task whether the model uses it or not.
    """
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    _write_skill(tmp_path, "add-tool", "Use when adding a tool")
    agent = _agent(SkillRegistry(builtin_dir=tmp_path))

    block = await _skills_block(agent, "add a database migration")

    assert "- add-tool: Use when adding a tool" in block
    assert "- db-migration: Use when adding a database migration" in block
    assert "BODY-" not in block
    assert "use_skill" in block  # the model is told how to fetch it
    assert "advisory" in block.lower()  # framed as advisory, not authoritative


async def test_the_list_is_the_same_text_for_every_request(tmp_path):
    """No per-request ranking: an unrelated request sees the same skills as a matching one."""
    for name in ("delta", "alpha", "charlie", "bravo"):
        _write_skill(tmp_path, name, f"Use when doing {name}")
    agent = _agent(SkillRegistry(builtin_dir=tmp_path))

    matching = await _skills_block(agent, "doing alpha")
    unrelated = await _skills_block(agent, "totally unrelated banana")

    assert matching == unrelated
    assert [line[2:].split(":")[0] for line in matching.splitlines() if line.startswith("- ")] == [
        "alpha",
        "bravo",
        "charlie",
        "delta",
    ]


async def test_no_skill_is_selected_by_similarity_for_an_ordinary_task(tmp_path):
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    agent = _agent(SkillRegistry(builtin_dir=tmp_path))

    assert await agent._select_skills("add a database migration") == []


async def test_every_skill_is_listed_however_many_there_are(tmp_path):
    """The list is how the agent finds a skill; it used to stop at twelve of eighteen."""
    for n in range(45):
        _write_skill(tmp_path, f"skill-{n:02d}", f"Use for job number {n}")
    agent = _agent(SkillRegistry(builtin_dir=tmp_path))

    block = await agent._load_skills_block(AgentPayload(task_id="t", prompt="anything"), [])

    assert all(f"- skill-{n:02d}: Use for job number {n}" in block for n in range(45))


async def test_flow_required_skill_is_loaded_in_full_as_the_execution_contract(tmp_path):
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    _write_skill(tmp_path, "add-tool", "Use when adding a tool")
    agent = _agent(SkillRegistry(builtin_dir=tmp_path))
    payload = AgentPayload(task_id="flow-step", prompt="an unrelated prompt", skills=["db-migration"])

    selected = await agent._select_skills(payload.prompt, payload.skills)
    block = await agent._load_skills_block(payload, selected)

    assert [skill.name for skill in selected] == ["db-migration"]
    assert "Required flow procedures" in block
    assert "BODY-db-migration" in block
    assert "not optional suggestions" in block
    assert "add-tool" not in block


async def test_a_flow_step_gets_its_required_skill_through_load_context(tmp_path):
    """Called on its own, _load_context gives the step's required skill, not a list."""
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    _write_skill(tmp_path, "add-tool", "Use when adding a tool")
    agent = _agent(SkillRegistry(builtin_dir=tmp_path))

    ctx = await agent._load_context(AgentPayload(task_id="t", prompt="step", skills=["db-migration"]))

    assert "BODY-db-migration" in ctx and "add-tool" not in ctx


async def test_no_skills_returns_empty(tmp_path):
    agent = _agent(SkillRegistry(builtin_dir=tmp_path))  # empty dir
    assert await _skills_block(agent, "anything") == ""


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
    """An engineering-only skill never reaches a general agent's list; a general one does."""
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")  # engineering (default)
    _write_skill(tmp_path, "add-tool", "Use when adding a tool", domains=["general"])
    general = _agent(SkillRegistry(builtin_dir=tmp_path), domain="general")

    block = await _skills_block(general, "add a database migration")

    assert "db-migration" not in block
    assert "- add-tool: Use when adding a tool" in block


async def test_engineering_agent_still_gets_engineering_skills(tmp_path):
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    ctx = await _agent(SkillRegistry(builtin_dir=tmp_path))._load_context(
        AgentPayload(task_id="t", prompt="add a database migration")
    )
    assert "db-migration" in ctx
    assert "Use when adding a database migration" in ctx


async def test_a_skill_loaded_with_use_skill_is_recorded_for_the_run(tmp_path):
    """Skill retirement and the run's skill list read this record; a suggestion used to make it."""
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    store = _RunStore()
    agent = _agent(SkillRegistry(builtin_dir=tmp_path), agent_run_store=store)
    payload = AgentPayload(task_id="t", prompt="add a database migration")

    await agent.record_skill_loaded(payload, "db-migration")
    await agent.record_skill_loaded(payload, "no-such-skill")

    assert [[s["name"] for s in skills] for skills in store.skills] == [["db-migration"]]


async def test_at_most_two_distinct_skills_are_selected(tmp_path):
    """The selector still serves the coding briefing: a primary plus a distinct secondary."""
    for name in ("db-migration", "db-schema", "db-index", "unrelated-topic"):
        _write_skill(tmp_path, name, f"Use when working on {name.replace('-', ' ')}")
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed, min_similarity=0.0)

    selected = await selector.select("db migration schema index")
    assert len(selected) <= 2


async def test_closest_finds_the_most_similar_other_skill_without_caching_the_draft(tmp_path):
    _write_skill(tmp_path, "db-migration", "Use when adding a database migration")
    _write_skill(tmp_path, "add-tool", "Use when adding a tool")
    registry = SkillRegistry(builtin_dir=tmp_path)
    selector = SkillSelector(registry, embed_fn=_fake_embed)
    draft = SimpleNamespace(name="new-migration", description="Use when writing a schema migration")

    score, skill = await selector.closest(draft, registry.all())

    assert skill.name == "db-migration" and score > 0.99
    assert "new-migration" not in (selector._embeddings or {})
    assert await selector.closest(draft, []) is None
