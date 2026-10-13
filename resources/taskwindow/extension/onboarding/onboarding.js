// First-install guide, opened only when no daemon answered at install time.
// It reflects the live connection so the user sees it go green after running
// the CLI, without reloading.
const $ = (id) => document.getElementById(id);

function render(connected) {
  $("dot").className = `dot ${connected ? "on" : "off"}`;
  $("status").textContent = connected ? "Connected to the daemon ✓" : "Waiting for the daemon — run the two commands below.";
  $("guide").hidden = connected;
  $("done").hidden = !connected;
}

$("settings").addEventListener("click", (event) => {
  event.preventDefault();
  chrome.runtime.openOptionsPage();
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type === "taskwindow:status") render(msg.connected === true);
});

chrome.runtime
  .sendMessage({ type: "taskwindow:getStatus" })
  .then((res) => render(res?.connected === true))
  .catch(() => render(false));
