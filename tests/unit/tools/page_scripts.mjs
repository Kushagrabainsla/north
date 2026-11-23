// Runs evaluateScript against a real V8 through Node's inspector, which speaks
// the same Runtime protocol as Chrome. Prints one JSON line per case; the
// Python test reads them (test_page_scripts.py).
import inspector from "node:inspector/promises";

import { READ_ONLY_MARKER, evaluateReadOnly, evaluateScript } from "../../../resources/taskwindow/extension/tools/evaluate.js";

const session = new inspector.Session();
session.connect();
const calls = [];
const send = (method, params) => {
  calls.push(params.expression);
  return session.post(method, params);
};

const cases = {
  lastExpression: "const a = 2;\na * 21",
  functionBody: "const b = 40;\nreturn b + 2;",
  functionBodyWithAwait: "const c = await Promise.resolve(41);\nreturn c + 1;",
  // Throws a SyntaxError while running, after its first statement ran: never run twice.
  runtimeSyntaxError: "globalThis.runs = (globalThis.runs || 0) + 1;\nJSON.parse('{')",
  broken: "let = = 2",
};

for (const [name, code] of Object.entries(cases)) {
  calls.length = 0;
  const reply = await evaluateScript(send, code);
  const details = reply.exceptionDetails;
  console.log(
    JSON.stringify({
      name,
      value: reply.result?.value ?? null,
      error: details ? details.exception?.description ?? details.text : null,
      scriptRuns: calls.filter((expression) => expression.includes(code)).length,
    }),
  );
}
console.log(JSON.stringify({ name: "runtimeSyntaxErrorCount", value: globalThis.runs ?? 0 }));

// Read-only scripts: each runs, or is stopped before it changes anything.
globalThis.page = { title: "Ada", items: ["a", "b"] };
const readOnly = {
  roLastExpression: "const t = globalThis.page.title;\nt + '!'",
  // Chrome also lets a script build arrays and maps of its own (checked on real scripts); Node's V8 is
  // stricter about new objects, so the cases here read plain values.
  roFunctionBody: "const n = globalThis.page.items.length;\nreturn n * 21;",
  // A promise schedules work, so an await is stopped too: such a script asks, as before.
  roAwait: "const v = await Promise.resolve(globalThis.page.items.length);\nreturn v;",
  roWritesGlobal: "globalThis.page.title = 'changed';\n'done'",
  roNewGlobal: "globalThis.leaked = 1;\n'done'",
  roTimer: "setTimeout(() => {}, 0);\n'done'",
};
for (const [name, code] of Object.entries(readOnly)) {
  const reply = await evaluateReadOnly(send, code);
  const details = reply.exceptionDetails;
  console.log(
    JSON.stringify({
      name,
      value: reply.result?.value ?? null,
      error: details ? details.exception?.description ?? details.text : null,
    }),
  );
}
console.log(
  JSON.stringify({ name: "roNothingChanged", value: globalThis.page.title === "Ada" && !("leaked" in globalThis) }),
);

// An extension that does not know the marker runs the script the ordinary way: it must run none of it.
const marked = `${READ_ONLY_MARKER}globalThis.page.title = 'changed by an old extension';\n'done'`;
const old = await evaluateScript(send, marked);
console.log(
  JSON.stringify({
    name: "oldExtensionRunsNothing",
    value: globalThis.page.title === "Ada",
    error: old.exceptionDetails?.exception?.className ?? null,
  }),
);
console.log(
  JSON.stringify({
    name: "markersLeft",
    value: Object.keys(globalThis).filter((key) => key.startsWith("__north_script_ran_")).length,
  }),
);
session.disconnect();
