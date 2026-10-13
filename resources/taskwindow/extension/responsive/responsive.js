// Viewport harness: one fixed-size iframe per requested viewport. An iframe's
// viewport is its own box, so media queries and vw/vh resolve exactly — no
// window minimum-size interference. The background script asserts each frame's
// real innerWidth via chrome.scripting (allFrames) and applies header-stripping
// DNR rules scoped to this tab so framed pages load.

const params = new URLSearchParams(location.search);
const target = params.get("url") || "about:blank";
let widths = [];
try {
  widths = JSON.parse(params.get("widths") || "[]");
} catch {}

for (const v of widths) {
  const fig = document.createElement("figure");
  const cap = document.createElement("figcaption");
  cap.textContent = `${v.width} × ${v.height}`;
  const frame = document.createElement("iframe");
  frame.src = target;
  frame.width = v.width;
  frame.height = v.height;
  frame.setAttribute("data-vp-width", String(v.width));
  fig.appendChild(cap);
  fig.appendChild(frame);
  document.body.appendChild(fig);
}
