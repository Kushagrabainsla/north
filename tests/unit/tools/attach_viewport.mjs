// Loads the extension's cdp.js with a stand-in `chrome` and attaches to a tab, as the
// extension does before it drives one. Prints the CDP commands sent, as JSON, for
// test_attach_viewport.py.
const sent = [];
globalThis.chrome = {
  debugger: {
    onEvent: { addListener() {} },
    onDetach: { addListener() {} },
    attach: async () => {},
    sendCommand: async (target, method, params) => {
      sent.push({ tabId: target.tabId, method, params: params ?? null });
      return {};
    },
  },
  tabs: { sendMessage: async () => {} },
};

const { ensureAttached } = await import("../../../resources/taskwindow/extension/tools/cdp.js");
await ensureAttached(7);
await ensureAttached(7); // already attached: nothing is sent again
console.log(JSON.stringify(sent));
