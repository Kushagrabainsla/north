// Runs evaluateScript against a real V8 through Node's inspector, which speaks
// the same Runtime protocol as Chrome. Prints one JSON line per case; the
// Python test reads them (test_page_scripts.py).
import inspector from "node:inspector/promises";

import { evaluateScript } from "../../../resources/taskwindow/extension/tools/evaluate.js";

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
console.log(
  JSON.stringify({
    name: "markersLeft",
    value: Object.keys(globalThis).filter((key) => key.startsWith("__north_script_ran_")).length,
  }),
);
session.disconnect();
