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

async function markerWasSet(send, marker) {
  const key = JSON.stringify(marker);
  const reply = await send("Runtime.evaluate", {
    expression: `(() => { const ran = ${key} in globalThis; delete globalThis[${key}]; return ran; })()`,
    returnByValue: true,
  });
  return reply.result?.value === true;
}
