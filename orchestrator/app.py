"""FastAPI application entry point for the Orchestrator server (port 8000).

See docs/CODING_STYLE.md Sections 10.4, 12, 17.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from agents.models import AgentDependencies
from agents.registry import AgentRegistry
from approval.approval_memory import ApprovalMemory
from approval.callback_server import app as callback_app
from approval.judgement_filter import JudgementFilter
from approval.tui import TUIAwareNotifier
from approval.unattended import UnattendedPolicy
from bootstrap.onboarding import run_bootstrap_if_needed
from config.dependencies import build_production_dependencies
from config.settings import settings
from gateways.telegram import TelegramGateway
from jobs.models import Job
from jobs.scheduler import V1_CRON_ENTRIES, CronScheduler
from ledger.models import LedgerEntry, LedgerSource, LedgerStatus
from memory.consolidator import EpisodeConsolidator
from memory.embeddings import EmbeddingIndex
from memory.extraction import ExtractionPipeline
from memory.injection import ContextInjector
from orchestrator.api import configure as configure_api
from orchestrator.api import health_router, webhook_router
from orchestrator.api import router as orchestrator_router
from orchestrator.constants import WATCHDOG_POLL_INTERVAL_SECONDS
from orchestrator.exceptions import TaskCapacityError
from orchestrator.failure_handler import FailureHandler
from orchestrator.models import TaskRequest
from orchestrator.north_star import NorthStarChecker
from orchestrator.orchestrator import Orchestrator
from orchestrator.reconcile import recover_interrupted_tasks
from orchestrator.router import ExecutionPlanner
from orchestrator.synthesizer import ResultSynthesizer
from orchestrator.watchdog import watch_stuck_tasks
from skills import SkillRegistry, SkillSelector
from skills.distiller import SkillDistiller
from tools.confidence import RELIABLE_TOOLS
from tools.registry import ToolRegistry
from tools.semantic.search_code import SearchCodeTool
from tools.specialized._sandbox import SandboxConfig
from tools.specialized.bash import BashTool
from tools.specialized.gh_tool import GhTool
from tools.specialized.git_tool import GitTool
from tools.specialized.kasa_tool import KasaTool
from tools.specialized.patch_file import PatchFileTool
from tools.specialized.shell_tool import ShellTool
from tools.tool_index import ToolIndex
from tools.universal.create_agent import CreateAgentTool
from tools.universal.create_skill import CreateSkillTool
from tools.universal.create_tool import CreateToolTool
from tools.universal.get_active_sessions import GetActiveSessionsTool
from tools.universal.get_task_status import GetTaskStatusTool
from tools.universal.query_metrics import QueryMetricsTool
from tools.universal.schedule_task import ScheduleTaskTool
from tools.universal.update_plan import UpdatePlanTool
from tools.universal.use_skill import UseSkillTool
from utils.logging import configure_structured_logging
from utils.security import load_secret
from utils.tasks import drain
from utils.time import utcnow
from utils.version import NORTH_VERSION
from web.api import configure as configure_web
from web.api import router as web_api_router
from web.api import session_router as web_session_router

logger = logging.getLogger(__name__)

_AGENTS_DIR = Path(__file__).parent.parent / "agents"
# Built-in skills ship as content under the skills package; learned skills are
# distilled at runtime into the user's north_home (kept out of the repo).
_BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills" / "builtin"

# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


def _step(msg: str) -> None:
    log_file = os.environ.get("NORTH_LOG_FILE", "").strip()
    if log_file:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"  [startup] {msg}\n")
    else:
        print(f"  [startup] {msg}", flush=True, file=sys.stderr)


def _validate_config() -> None:
    from inference.registry import PROVIDER_DEFINITIONS

    if not any(definition.is_configured(settings) for definition in PROVIDER_DEFINITIONS):
        raise RuntimeError("No inference provider is configured. Run `north start` to configure one.")


def _attach_tui_notifier(deps) -> None:
    # Suppress macOS/terminal alerts while the TUI is connected - the global
    # SSE stream handles approvals inline.
    deps.notifier = TUIAwareNotifier(
        stream_manager=deps.stream_manager,
        fallback=deps.notifier,
    )


def _attach_embedding_index(deps) -> None:
    embedding_index = EmbeddingIndex(
        db_path=settings.north_home / "embeddings.db",
        embed_fn=deps.embed_fn,
    )
    deps.context_store.attach_embedding_index(embedding_index)


def _build_tool_registry(
    deps, tool_graph, judgement_filter: JudgementFilter | None = None
) -> tuple[ToolRegistry, CreateAgentTool]:
    tool_registry = ToolRegistry(graph=tool_graph, auto_register=True)
    # Live approval mode: read from NorthSettings at decision time so a runtime change
    # (via the settings API) takes effect immediately, no restart.
    mode_provider = lambda: deps.north_settings.autonomy  # noqa: E731
    tool_registry.register(ScheduleTaskTool(job_processor=deps.job_processor, cron_store=deps.cron_store))
    tool_registry.make_universal("schedule_task")
    # create/update actions are gated behind a user approval card inside the
    # tool itself, so every entry point (agent loop, delegation, direct-tool
    # execution) sees the same gate.
    tool_registry.register(
        CreateToolTool(
            tool_registry=tool_registry,
            approval_store=deps.approval_store,
            stream_manager=deps.stream_manager,
            approval_timeout_seconds=deps.north_settings.approval_timeout_seconds,
            judgement_filter=judgement_filter,
            notifier=deps.notifier,
        )
    )
    create_agent_tool = CreateAgentTool(cron_store=deps.cron_store)
    tool_registry.register(create_agent_tool)
    tool_registry.make_universal("create_agent")
    tool_registry.register(QueryMetricsTool(ledger=deps.ledger))
    tool_registry.make_universal("query_metrics")
    tool_registry.register(GetTaskStatusTool(ledger=deps.ledger))
    tool_registry.make_universal("get_task_status")
    tool_registry.register(GetActiveSessionsTool(running_task_store=deps.running_task_store))
    tool_registry.make_universal("get_active_sessions")
    tool_registry.register(UpdatePlanTool(plan_store=deps.plan_store, stream_manager=deps.stream_manager))
    tool_registry.make_universal("update_plan")
    # Semantic code search (#2) - only when embeddings are available.
    if deps.code_index is not None:
        tool_registry.register(SearchCodeTool(code_index=deps.code_index))
        tool_registry.make_universal("search_code")
    # BashTool and ShellTool gate every command behind user approval and cannot
    # be auto-discovered (they need the ApprovalStore injected at startup).
    tool_registry.register(
        BashTool(
            approval_store=deps.approval_store,
            stream_manager=deps.stream_manager,
            approval_timeout_seconds=deps.north_settings.approval_timeout_seconds,
            judgement_filter=judgement_filter,
            notifier=deps.notifier,
            sandbox=SandboxConfig.from_settings(settings),
            unattended=UnattendedPolicy.from_settings(settings),
            mode_provider=mode_provider,
        )
    )
    tool_registry.register(
        ShellTool(
            approval_store=deps.approval_store,
            stream_manager=deps.stream_manager,
            approval_timeout_seconds=deps.north_settings.approval_timeout_seconds,
            judgement_filter=judgement_filter,
            notifier=deps.notifier,
        )
    )
    # Override the auto-discovered (immediate) PatchFileTool with one that previews
    # a unified diff in an approval card before writing.
    tool_registry.register(
        PatchFileTool(
            approval_store=deps.approval_store,
            stream_manager=deps.stream_manager,
            approval_timeout_seconds=deps.north_settings.approval_timeout_seconds,
            judgement_filter=judgement_filter,
            notifier=deps.notifier,
            unattended=UnattendedPolicy.from_settings(settings),
            mode_provider=mode_provider,
        )
    )
    # Override the auto-discovered (gate-less, fail-closed) GitTool/GhTool/KasaTool
    # with instances wired to the approval flow so their mutating actions surface
    # approval cards instead of being refused outright.
    unattended_policy = UnattendedPolicy.from_settings(settings)
    for tool_cls in (GitTool, GhTool, KasaTool):
        kwargs: dict = {
            "approval_store": deps.approval_store,
            "stream_manager": deps.stream_manager,
            "approval_timeout_seconds": deps.north_settings.approval_timeout_seconds,
            "judgement_filter": judgement_filter,
            "notifier": deps.notifier,
        }
        # Only the local git tool honours unattended auto-approval; gh (network) and
        # kasa (device control) always require a human.
        if tool_cls is GitTool:
            kwargs["unattended"] = unattended_policy
            kwargs["mode_provider"] = mode_provider
        tool_registry.register(tool_cls(**kwargs))
    return tool_registry, create_agent_tool


def _build_skills(deps) -> tuple[SkillRegistry, SkillSelector]:
    """Load built-in + learned skills and build the semantic selector.

    Learned skills live under north_home so runtime-distilled procedures are the
    user's own data, never committed to the repo alongside the built-in ones.
    NORTH_BUILTIN_SKILLS_DIR overrides the built-in location; it exists only so the
    eval harness can A/B skills-on vs skills-off by pointing at an empty directory.
    """
    builtin_dir = Path(os.environ.get("NORTH_BUILTIN_SKILLS_DIR") or _BUILTIN_SKILLS_DIR)
    registry = SkillRegistry(
        builtin_dir=builtin_dir,
        learned_dir=settings.north_home / "skills",
    )
    selector = SkillSelector(registry, embed_fn=deps.embed_fn)
    return registry, selector


def _build_tool_index(deps) -> ToolIndex | None:
    if deps.embed_fn is None:
        return None
    return ToolIndex(
        db_path=settings.north_home / "tool_index.db",
        embed_fn=deps.embed_fn,
    )


async def _populate_tool_index(tool_index: ToolIndex, tool_registry: ToolRegistry) -> None:
    """Embed every registered tool description so agents can do semantic selection.

    One batched call, and it runs *after* the server starts accepting requests: a
    first boot would otherwise embed every tool before answering anything. Until it
    finishes, `search_tools` returns nothing and agents fall back to their full tool
    set - correct, just not semantically ranked.
    """
    indexed = await tool_index.update_tools([(tool.name, tool.description) for tool in tool_registry.all_tools()])
    if indexed:
        logger.info("Tool index: embedded %d new or changed tool description(s)", indexed)


def _build_agent_deps(deps, tool_registry: ToolRegistry) -> AgentDependencies:
    return AgentDependencies(
        context_store=deps.context_store,
        inference_router=deps.cost_tracker,
        tool_registry=tool_registry,
        confidence_tracker=deps.confidence_tracker,
        stream_manager=deps.stream_manager,
        episodic_store=deps.episodic_store,
        approval_store=deps.approval_store,
        notifier=deps.notifier,
        fact_store=deps.fact_store,
        memory=deps.memory,
        ledger=deps.ledger,
        agent_max_iterations=settings.agent_max_iterations,
        agent_history_keep_recent=settings.agent_history_keep_recent,
        approval_timeout_seconds=deps.north_settings.approval_timeout_seconds,
        running_task_store=deps.running_task_store,
        plan_store=deps.plan_store,
        north_settings=deps.north_settings,
        agent_run_store=deps.agent_run_store,
    )


def _build_agent_registry(agent_deps: AgentDependencies) -> AgentRegistry:
    registry = AgentRegistry(agents_dir=_AGENTS_DIR, deps=agent_deps)
    # Break the circular dependency: agents need the registry to delegate sub-tasks,
    # but the registry needs agent_deps to instantiate agents.
    agent_deps.agent_registry = registry
    return registry


def _build_extraction_pipeline(deps) -> ExtractionPipeline:
    return ExtractionPipeline(
        ledger=deps.ledger,
        context_store=deps.context_store,
        inference_router=deps.cost_tracker,
        north_home=settings.north_home,
        poll_interval_seconds=settings.extraction_poll_interval_seconds,
        max_daily_cost_usd=settings.extraction_max_daily_cost_usd,
        min_output_chars=settings.extraction_min_output_chars,
        max_concurrent=settings.extraction_max_concurrent,
        fact_store=deps.fact_store,
    )


def _build_orchestrator(
    deps,
    agent_registry: AgentRegistry,
    tool_registry: ToolRegistry,
    extraction_pipeline: ExtractionPipeline,
    judgement_filter: JudgementFilter,
    approval_memory: ApprovalMemory,
) -> Orchestrator:
    return Orchestrator(
        ledger=deps.ledger,
        agent_registry=agent_registry,
        north_star_checker=NorthStarChecker(
            memory=deps.memory,
            inference_router=deps.cost_tracker,
        ),
        execution_planner=ExecutionPlanner(
            agent_registry=agent_registry,
            inference_router=deps.cost_tracker,
            tool_registry=tool_registry,
            workspace=settings.north_workspace,
            north_settings=settings,
        ),
        task_context_store=deps.task_context_store,
        failure_handler=FailureHandler(
            ledger_writer=deps.ledger,
            task_context_store=deps.task_context_store,
            stream_manager=deps.stream_manager,
        ),
        notifier=deps.notifier,
        stream_manager=deps.stream_manager,
        approval_store=deps.approval_store,
        judgement_filter=judgement_filter,
        north_settings=deps.north_settings,
        synthesizer=ResultSynthesizer(inference_router=deps.cost_tracker, memory=deps.memory),
        tracked_router=deps.cost_tracker,
        episodic_store=deps.episodic_store,
        tool_registry=tool_registry,
        default_workspace=settings.north_workspace,
        extraction_pipeline=extraction_pipeline,
        worktree_isolation=settings.worktree_isolation_enabled,
        worktree_root=settings.worktree_root,
        best_of_n=settings.best_of_n,
        best_of_n_test_command=settings.best_of_n_test_command,
        verify_command=settings.verify_command,
        running_task_store=deps.running_task_store,
        stuck_task_max_age_seconds=settings.stuck_task_max_age_seconds,
        self_repair=settings.self_repair_enabled,
        idempotency_window_seconds=settings.idempotency_window_seconds,
        critic=settings.critic_enabled,
        approval_memory=approval_memory,
        plan_store=deps.plan_store,
    )


def _build_context_injector(deps) -> ContextInjector:
    return ContextInjector(
        context_store=deps.context_store,
        inference_router=deps.cost_tracker,
        ledger=deps.ledger,
    )


def _configure_routers(app, orchestrator, deps, agent_registry, context_injector, skill_registry) -> None:
    configure_api(
        app,
        orchestrator=orchestrator,
        stream_manager=deps.stream_manager,
        ledger=deps.ledger,
        agent_registry=agent_registry,
        context_store=deps.context_store,
        context_injector=context_injector,
        job_processor=deps.job_processor,
        inference_router=deps.inference_router,
        confidence_tracker=deps.confidence_tracker,
        cron_store=deps.cron_store,
        north_settings=deps.north_settings,
        agent_run_store=deps.agent_run_store,
    )
    configure_web(
        app,
        orchestrator=orchestrator,
        ledger=deps.ledger,
        agent_registry=agent_registry,
        job_processor=deps.job_processor,
        cron_store=deps.cron_store,
        approval_store=deps.approval_store,
        north_settings=deps.north_settings,
        agent_run_store=deps.agent_run_store,
        north_home=settings.north_home,
        fact_store=deps.fact_store,
        inference_router=deps.inference_router,
        skill_registry=skill_registry,
    )


def _build_callback_server() -> uvicorn.Server:
    config = uvicorn.Config(callback_app, host="127.0.0.1", port=8001, log_level="warning")
    server = uvicorn.Server(config)
    # Prevents the nested uvicorn from overriding the outer server's SIGTERM/SIGINT handlers on macOS.
    server.install_signal_handlers = lambda: None
    return server


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


async def _guarded(coro, name: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("background task %r failed", name)


async def _pool_refresh_loop(deps, orchestrator: Orchestrator | None = None) -> None:
    interval = settings.inference_pool_refresh_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            await deps.inference_router.refresh_pools()
            logger.info("Inference pool refreshed successfully")
            if orchestrator is not None:
                orchestrator.notify_model_recovery()
        except Exception:
            logger.warning("Inference pool refresh failed", exc_info=True)


def _launch_background_tasks(
    deps,
    orchestrator: Orchestrator,
    extraction_pipeline: ExtractionPipeline,
    skill_distiller: SkillDistiller,
    callback_server: uvicorn.Server,
    telegram_gateway: TelegramGateway | None = None,
) -> list[asyncio.Task]:
    async def _dispatch_job(job: Job) -> None:
        if job.task == "task_context_cleanup":
            n = await deps.task_context_store.cleanup_stale_tasks(
                active_task_ids=orchestrator.active_task_ids,
            )
            now = utcnow()
            completed_before = now - datetime.timedelta(days=settings.task_cleanup_completed_days)
            failed_before = now - datetime.timedelta(days=settings.task_cleanup_failed_days)
            pruned = await deps.ledger.prune(completed_before, failed_before)
            await deps.ledger.write(
                LedgerEntry.new(
                    source=LedgerSource.SYSTEM,
                    action=(f"task_context_cleanup: removed {n} stale rows, pruned {pruned} ledger entries"),
                    status=LedgerStatus.COMPLETED,
                )
            )
            return
        await orchestrator.submit_task(TaskRequest(prompt=f"[scheduled] {job.task}", source=LedgerSource.CRON))

    cron_scheduler = CronScheduler(
        processor=deps.job_processor,
        entries=V1_CRON_ENTRIES,
        cron_store=deps.cron_store,
    )

    episode_consolidator = EpisodeConsolidator(
        ledger=deps.ledger,
        episodic_store=deps.episodic_store,
        inference_router=deps.cost_tracker,
        north_home=settings.north_home,
    )

    tasks = [
        asyncio.create_task(
            _guarded(
                deps.job_processor.run(
                    on_job=_dispatch_job,
                    poll_interval_seconds=settings.job_poll_interval_seconds,
                ),
                "job_processor",
            ),
            name="job_processor",
        ),
        asyncio.create_task(_guarded(cron_scheduler.run(), "cron_scheduler"), name="cron_scheduler"),
        asyncio.create_task(_guarded(extraction_pipeline.run(), "extraction_pipeline"), name="extraction_pipeline"),
        asyncio.create_task(_guarded(episode_consolidator.run(), "episode_consolidator"), name="episode_consolidator"),
        asyncio.create_task(_guarded(skill_distiller.run(), "skill_distiller"), name="skill_distiller"),
        asyncio.create_task(_guarded(callback_server.serve(), "callback_server"), name="callback_server"),
        asyncio.create_task(_guarded(_pool_refresh_loop(deps, orchestrator), "pool_refresh"), name="pool_refresh"),
        asyncio.create_task(
            _guarded(orchestrator.drain_queued_tasks_loop(), "task_queue_drainer"),
            name="task_queue_drainer",
        ),
        asyncio.create_task(
            _guarded(
                watch_stuck_tasks(
                    orchestrator,
                    poll_interval=WATCHDOG_POLL_INTERVAL_SECONDS,
                    max_age_seconds=settings.stuck_task_max_age_seconds,
                ),
                "stuck_task_watchdog",
            ),
            name="stuck_task_watchdog",
        ),
    ]

    # Telegram gateway — only if a bot token is configured
    if telegram_gateway is not None:
        tasks.append(
            asyncio.create_task(
                _guarded(telegram_gateway.run(), "telegram_gateway"),
                name="telegram_gateway",
            )
        )

    # Async first-run bootstrap — scans user files and seeds fact store.
    # Runs in the background so it never delays the user's first prompt.
    tasks.append(
        asyncio.create_task(
            _guarded(
                run_bootstrap_if_needed(
                    fact_store=deps.fact_store,
                    inference_router=deps.inference_router,
                    north_home=settings.north_home,
                ),
                "bootstrap",
            ),
            name="bootstrap",
        )
    )

    return tasks


async def _shutdown(
    deps,
    callback_server: uvicorn.Server,
    background_tasks: list[asyncio.Task],
    tool_registry: ToolRegistry | None = None,
) -> None:
    callback_server.should_exit = True
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    # Fire-and-forget writes (ledger entries, agent-run events) are not awaited by
    # their callers, so give them a chance to land before connections close.
    await drain()
    if tool_registry is not None:
        await tool_registry.aclose()
    await deps.cost_tracker.aclose()


def _warn_unknown_cron_agents(agent_registry: AgentRegistry) -> None:
    known = set(agent_registry.names())
    for entry in V1_CRON_ENTRIES:
        if entry.agent not in known and entry.agent != "system":
            logger.warning(
                "V1 cron entry %r references unknown agent %r - job will fail at dispatch",
                entry.name,
                entry.agent,
            )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_structured_logging()

    _step("loading secret")
    load_secret()
    _validate_config()

    _step("building dependencies")
    deps = build_production_dependencies()
    _attach_tui_notifier(deps)
    # Expose the live container so runtime config changes (north_config set)
    # can rebuild the inference router in place without a restart.
    from config.runtime import set_runtime

    set_runtime(deps)

    _step("building embedding index")
    _attach_embedding_index(deps)

    _step("building tool registry")
    tool_graph = AgentRegistry.build_tool_graph(_AGENTS_DIR)
    approval_memory = ApprovalMemory(settings.north_home / "approval_memory.db")
    judgement_filter = JudgementFilter(
        memory=deps.memory,
        inference_router=deps.cost_tracker,
        approval_memory=approval_memory,
        mode_provider=lambda: deps.north_settings.autonomy,
    )
    tool_registry, create_agent_tool = _build_tool_registry(deps, tool_graph, judgement_filter)

    _step("loading skills")
    skill_registry, skill_selector = _build_skills(deps)
    tool_registry.register(UseSkillTool(skill_registry))
    tool_registry.make_universal("use_skill")
    tool_registry.register(CreateSkillTool(skill_registry))
    tool_registry.make_universal("create_skill")

    _step("seeding confidence defaults")
    await deps.confidence_tracker.seed_defaults(tool_graph, RELIABLE_TOOLS)

    _step("refreshing inference pools")
    await deps.inference_router.refresh_pools()

    # Built now so agents can hold the reference; populated in the background
    # below, once the server is already serving.
    _step("building tool index")
    tool_index = _build_tool_index(deps)

    _step("scanning agent registry")
    agent_deps = _build_agent_deps(deps, tool_registry)
    agent_deps.tool_index = tool_index
    agent_deps.skill_registry = skill_registry
    agent_deps.skill_selector = skill_selector
    agent_registry = _build_agent_registry(agent_deps)
    create_agent_tool._agent_registry = agent_registry  # late-wire after registry is built
    _step(f"registered agents: {agent_registry.names()}")
    _warn_unknown_cron_agents(agent_registry)

    extraction_pipeline = _build_extraction_pipeline(deps)
    orchestrator = _build_orchestrator(
        deps, agent_registry, tool_registry, extraction_pipeline, judgement_filter, approval_memory
    )
    # Share the orchestrator's JudgementFilter with agents so request_approval
    # calls skip the user prompt when a learned rule already covers the situation.
    agent_deps.judgement_filter = orchestrator._judgement_filter
    context_injector = _build_context_injector(deps)

    _step("running startup recovery sweep")
    await recover_interrupted_tasks(
        deps,
        orchestrator,
        max_age_seconds=settings.stuck_task_max_age_seconds,
        resume_side_effecting=settings.resume_side_effecting_tasks,
    )

    _step("configuring API router")
    _configure_routers(app, orchestrator, deps, agent_registry, context_injector, skill_registry)

    _step("configuring callback server")
    callback_server = _build_callback_server()

    _step("scheduling background tasks")
    skill_distiller = SkillDistiller(
        episodic_store=deps.episodic_store,
        inference_router=deps.cost_tracker,
        skill_registry=skill_registry,
        skill_selector=skill_selector,
        learned_dir=settings.north_home / "skills",
    )
    telegram_gateway = TelegramGateway()
    background_tasks = _launch_background_tasks(
        deps,
        orchestrator,
        extraction_pipeline,
        skill_distiller,
        callback_server,
        telegram_gateway=telegram_gateway,
    )
    if tool_index is not None:
        background_tasks.append(
            asyncio.create_task(
                _guarded(_populate_tool_index(tool_index, tool_registry), "tool_index"),
                name="tool_index",
            )
        )

    _step("startup complete - yielding to server")
    try:
        yield
    finally:
        await _shutdown(deps, callback_server, background_tasks, tool_registry)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="north Orchestrator",
    description="Personal Life Operating System - core API",
    version=NORTH_VERSION,
    lifespan=lifespan,
)


@app.exception_handler(TaskCapacityError)
async def _task_capacity_handler(request: Request, exc: TaskCapacityError) -> JSONResponse:
    return JSONResponse(status_code=429, content={"detail": str(exc)})


app.include_router(health_router)
app.include_router(orchestrator_router)
app.include_router(webhook_router)
app.include_router(web_session_router)
app.include_router(web_api_router)

_WEB_DIST = Path(__file__).parent.parent / "web" / "dist"
if _WEB_DIST.exists():
    app.mount("/app", StaticFiles(directory=_WEB_DIST, html=True), name="north-web")
