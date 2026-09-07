"""The REPL the RLM strategies drive.

Zhang et al.'s key move is that the prompt never enters the model's context: it
is bound to a variable in a persistent environment, and the model writes code to
examine it. This is that environment.

Safety: the code executed here is written by a model, so the namespace is
restricted - no file access, no network, no exec, and imports limited to a
handful of pure-computation modules. That is enough for an eval on synthetic
data on your own machine. It is NOT enough for production; north's Docker
sandbox (tools/specialized/_sandbox.py) is what that would need.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import Awaitable, Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field

# What model-written code may call. Everything absent is absent on purpose.
_ALLOWED_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
        "float", "frozenset", "int", "isinstance", "len", "list", "map", "max",
        "min", "print", "range", "repr", "reversed", "round", "set", "setattr",
        "slice", "sorted", "str", "sum", "tuple", "type", "zip",
    )
}

# Modules model-written code may import. Without this, every trajectory burned
# its first turn on `import re` failing - a handicap on the REPL strategies
# specifically, which would have read as "the REPL approach is worse".
_IMPORTABLE = ("re", "json", "collections", "itertools", "math", "string", "statistics")


def _restricted_import(name: str, *args, **kwargs):
    root = name.split(".")[0]
    if root not in _IMPORTABLE:
        raise ImportError(f"{name!r} is not available here; allowed: {', '.join(_IMPORTABLE)}")
    return __import__(name, *args, **kwargs)


_CODE_BLOCK_RE = re.compile(r"```(?:repl|python)?\s*\n(.*?)```", re.DOTALL)
_FINAL_RE = re.compile(r"FINAL\((.*)\)\s*$", re.DOTALL)
_FINAL_VAR_RE = re.compile(r"FINAL_VAR\(\s*([A-Za-z_]\w*)\s*\)")

LlmQuery = Callable[[str], Awaitable[str]]


@dataclass
class ReplResult:
    stdout: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class ReplSession:
    """A namespace that survives across turns, with the corpus already bound."""

    context: str
    llm_query: LlmQuery | None = None
    timeout_s: float = 60.0
    namespace: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.namespace.update(
            {
                "__builtins__": {**_ALLOWED_BUILTINS, "__import__": _restricted_import},
                "context": self.context,
                "re": re,
                "json": json,
            }
        )
        if self.llm_query is not None:
            self.namespace["llm_query"] = self._sync_llm_query()

    def _sync_llm_query(self) -> Callable[[str], str]:
        """A blocking llm_query for the worker thread, answered on the event loop."""
        loop = asyncio.get_event_loop()

        def llm_query(prompt: str) -> str:
            assert self.llm_query is not None
            future = asyncio.run_coroutine_threadsafe(self.llm_query(str(prompt)), loop)
            return future.result(timeout=self.timeout_s)

        return llm_query

    async def run(self, code: str) -> ReplResult:
        """Execute one block, keeping whatever it defines for the next block."""
        return await asyncio.wait_for(asyncio.to_thread(self._run_sync, code), timeout=self.timeout_s)

    def _run_sync(self, code: str) -> ReplResult:
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                exec(code, self.namespace)  # noqa: S102 - restricted namespace, see module docstring
        except Exception as exc:
            return ReplResult(stdout=buffer.getvalue(), error=f"{type(exc).__name__}: {exc}")
        return ReplResult(stdout=buffer.getvalue())

    def final_answer(self) -> str | None:
        """The answer the code left behind, if it set one."""
        value = self.namespace.get("FINAL_ANSWER")
        return None if value is None else str(value)


def extract_code(reply: str) -> str:
    """The first fenced block in a reply, or "" when it wrote prose only."""
    match = _CODE_BLOCK_RE.search(reply)
    return match.group(1).strip() if match else ""


def extract_final(reply: str, session: ReplSession) -> str | None:
    """The final answer a reply declares, by literal or by variable name.

    Zhang et al. call this mechanism brittle (Appendix B) and they are right, so
    it is deliberately generous: a literal FINAL(...), a FINAL_VAR(name) naming a
    REPL variable, or an FINAL_ANSWER left in the namespace all count.
    """
    variable = _FINAL_VAR_RE.search(reply)
    if variable:
        value = session.namespace.get(variable.group(1))
        if value is not None:
            return str(value)
    literal = _FINAL_RE.search(reply.strip())
    if literal:
        return literal.group(1).strip().strip("\"'")
    return session.final_answer()
