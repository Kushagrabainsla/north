import { resolveTab } from "./tabs.js";
import { withDebugger, send } from "./cdp.js";

// Evaluated over CDP (Runtime.evaluate), the same path the DevTools console
// uses: it runs in the page's main world and is exempt from the page's Content
// Security Policy. The earlier chrome.scripting + eval(src) approach was
// subject to that CSP, so every site without 'unsafe-eval' (x.com, reddit.com,
// GitHub, …) rejected the tool outright.
export async function javascriptExecute({ code, awaitPromise = true, tabId, sessionToken }) {
  const tab = await resolveTab(tabId, sessionToken);
  return withDebugger(tab.id, async (tid) => {
    const { result, exceptionDetails } = await send(tid, "Runtime.evaluate", {
      expression: code,
      awaitPromise: awaitPromise !== false,
      returnByValue: true,
      userGesture: true,
      // REPL mode, as the DevTools console runs: a top-level `const ta = …`
      // from one call otherwise stays declared in the page for good, so the
      // next call reusing the name throws "Identifier 'ta' has already been
      // declared". It also allows top-level `await`.
      replMode: true,
    });
    if (exceptionDetails) {
      const detail = exceptionDetails.exception?.description || exceptionDetails.text || "Uncaught exception";
      throw new Error(`page script threw: ${detail}`);
    }
    let value = result?.value;
    if (value === undefined) {
      // Non-JSON results (undefined, functions, symbols, DOM nodes…): fall
      // back to the description, or null so the result stays serializable.
      value = result?.unserializableValue ?? result?.description ?? null;
    }
    return {
      data: { result: value },
      text: typeof value === "string" ? value : JSON.stringify(value, null, 2),
    };
  });
}
