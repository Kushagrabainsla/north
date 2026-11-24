// Run a page script over CDP the way the DevTools console does, and accept both
// ways a model writes one.
//
// A script runs in REPL mode (see javascript.js): its value is its last
// expression, and it may use top-level `await`. A model often writes a script
// as a function body instead, ending in `return x;`. In REPL mode that is
// "SyntaxError: Illegal return statement", and the first browser script failed
// that way five times (issue #82, item 7). Such a script is run again as the
// body of an async function.
//
// It is run again only when none of it ran, and that is proved, not guessed: the
// script first sets a marker on the page, so a SyntaxError with the marker unset
// was a compile error. A SyntaxError thrown while running (`JSON.parse("{")`)
// leaves the marker set, and is reported as it is; running that script again
// would repeat what it already did.
//
// `send(method, params)` sends one CDP command and returns its result.
export async function evaluateScript(send, code, { awaitPromise = true } = {}) {
  const marker = `__north_script_ran_${Math.random().toString(36).slice(2)}`;
  const first = await send("Runtime.evaluate", {
    expression: `globalThis[${JSON.stringify(marker)}] = true;\n${code}`,
    awaitPromise: awaitPromise !== false,
    returnByValue: true,
    userGesture: true,
    // REPL mode, as the DevTools console runs: a top-level `const ta = …`
    // from one call otherwise stays declared in the page for good, so the
    // next call reusing the name throws "Identifier 'ta' has already been
    // declared". It also allows top-level `await`.
    replMode: true,
  });
  const ran = await markerWasSet(send, marker);
  if (ran || first.exceptionDetails?.exception?.className !== "SyntaxError") {
    return first;
  }
  const asBody = await send("Runtime.evaluate", {
    expression: `(async () => {\n${code}\n})()`,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  // Not a function body either: the script is broken, and its first error says how.
  return asBody.exceptionDetails?.exception?.className === "SyntaxError" ? first : asBody;
}

// A script that starts with this marker only reads the page, and is run so that it cannot do anything
// else. North adds it; it can only make a script stricter. It is not valid JavaScript, so an older copy of
// this extension, which does not know it, fails to compile the script and runs none of it.
export const READ_ONLY_MARKER = "#north:read-only\n";

// Run a script in V8's side-effect-free mode (`throwOnSideEffect`), the mode DevTools uses to preview a
// value as you type. The script runs until it would change something: a click, a write to the page or
// to a variable outside the script, a timer, a request, storage. Then V8 stops it with
// "EvalError: Possible side-effect in debug-evaluate", before that step happens. So a script that ran
// to its end read the page and nothing else, and one that was stopped changed nothing.
//
// In this mode V8 throws away the optimised code of each function the script calls, and to find it
// walks all optimised code in the tab's renderer, old pages' included until they are collected. So
// north opens each page in a new tab (browser_taskwindow.py `_open`): an extension cannot ask Chrome to
// collect the garbage, as chrome.debugger has no HeapProfiler.
//
// A script still running after READ_ONLY_TIMEOUT_MS is stopped ("Execution was terminated"): it changed
// nothing, and north asks about it as about any script.
//
// It runs as a block first, so its top-level `const` and `let` stay inside it (declared at the top level
// they are a write to the page's scope); then as a function body, for `return x;`; then as an async
// function body, for `await`. Running one again is safe: the earlier try changed nothing.
export async function evaluateReadOnly(send, code) {
  const base = { returnByValue: true, throwOnSideEffect: true, timeout: READ_ONLY_TIMEOUT_MS };
  let reply;
  for (const [expression, extra] of [
    [`{\n${code}\n}`, {}],
    [`(() => {\n${code}\n})()`, {}],
    [`(async () => {\n${code}\n})()`, { awaitPromise: true }],
  ]) {
    reply = await send("Runtime.evaluate", { ...base, ...extra, expression });
    if (reply.exceptionDetails?.exception?.className !== "SyntaxError") return reply;
  }
  return reply;
}

export const READ_ONLY_TIMEOUT_MS = 10_000;

async function markerWasSet(send, marker) {
  const key = JSON.stringify(marker);
  const reply = await send("Runtime.evaluate", {
    expression: `(() => { const ran = ${key} in globalThis; delete globalThis[${key}]; return ran; })()`,
    returnByValue: true,
  });
  return reply.result?.value === true;
}
