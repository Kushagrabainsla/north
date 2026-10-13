/**
 * Visual "an agent is acting here" feedback: a phantom cursor that glides to
 * each interaction point and a pulsing inset glow, mirroring the reference
 * extension's layer. Both elements are pointer-events:none at max z-index;
 * motion respects prefers-reduced-motion. Injected on demand, top frame.
 *
 * Both stay visible for as long as the tab is being driven — no timed fade.
 * The cursor is DOM, so it appears in screenshots (CDP mouse events draw no
 * pointer), and an agent that moves the mouse, then screenshots to check the
 * spot before clicking, must still find it there. They hide only on an
 * explicit "hide" (sent when the debugger detaches from the tab).
 *
 * The cursor is the icon's blue texture seen through an arrow-shaped mask,
 * drifting slowly so it reads as alive while parked. It lives in a closed
 * shadow root so page CSS (`path { fill }`, `svg { width }`) cannot restyle it.
 */
(() => {
  if (globalThis.__agentTabIndicator) return;
  globalThis.__agentTabIndicator = true;

  // icons/icon128.png re-encoded as a q75 JPEG and inlined: a content script
  // cannot put extension files into the page without listing them as
  // web_accessible_resources, which lets any site fingerprint the extension.
  const TEXTURE = "data:image/jpeg;base64,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";

  // Tip at (0,0) in arrow units, drawn at SCALE with PAD units of margin, so
  // the tip sits TIP px into the element: translating by (x - TIP, y - TIP)
  // puts it on the exact point CDP clicks.
  const ARROW = "M0 0L0 16.2 4.3 12.3 7.1 18.6 9.9 17.4 7.2 11.2 12.8 11.2Z";
  const SCALE = 1.25;
  const PAD = 2;
  const TIP = PAD * SCALE;
  // One quadrant of the mirror-tiled texture, in arrow units (~112 CSS px).
  // Mirroring makes the tile seamless, so the drift can loop forever.
  const Q = 90;

  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const DRIFT = reduced
    ? ""
    : `<animate attributeName="x" from="0" to="${-2 * Q}" dur="12.5s" repeatCount="indefinite"/>` +
      `<animate attributeName="y" from="0" to="${-2 * Q}" dur="20.8s" repeatCount="indefinite"/>`;

  const CURSOR_SVG = `<svg class="arrow" width="${17 * SCALE}" height="${23 * SCALE}" viewBox="${-PAD} ${-PAD} 17 23" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <image id="tex" href="${TEXTURE}" width="${Q}" height="${Q}" preserveAspectRatio="none"/>
      <pattern id="fill" patternUnits="userSpaceOnUse" width="${2 * Q}" height="${2 * Q}">
        <use href="#tex"/>
        <use href="#tex" transform="matrix(-1 0 0 1 ${2 * Q} 0)"/>
        <use href="#tex" transform="matrix(1 0 0 -1 0 ${2 * Q})"/>
        <use href="#tex" transform="matrix(-1 0 0 -1 ${2 * Q} ${2 * Q})"/>
        ${DRIFT}
      </pattern>
    </defs>
    <path d="${ARROW}" fill="url(#fill)" stroke="#fff" stroke-width="1.15" stroke-linejoin="round"/>
  </svg>`;


  const SHADOW_CSS = `
    .cursor { position: fixed; top: 0; left: 0; opacity: 0; will-change: transform; }
    .arrow { display: block; overflow: visible; transform-origin: ${TIP}px ${TIP}px; transition: transform 90ms ease-out;
             filter: drop-shadow(0 1px 1.5px rgba(8,15,35,.45)) drop-shadow(0 3px 6px rgba(8,15,35,.18)); }
    .press .arrow { transform: scale(.86); }
  `;

  let cursor = null;
  let glow = null;
  let style = null;
  let visible = false;
  let at = { x: 0, y: 0 };

  function ensure() {
    if (cursor) return;
    style = document.createElement("style");
    style.id = "taskwindow-indicator-styles";
    style.textContent = `
      @keyframes taskwindow-pulse { 0%,100% { opacity:.5 } 50% { opacity:1 } }
      #taskwindow-glow-inner { animation: taskwindow-pulse 2s ease-in-out infinite; }
      @media (prefers-reduced-motion: reduce) { #taskwindow-glow-inner { animation: none; } }
    `;
    document.documentElement.appendChild(style);

    glow = document.createElement("div");
    glow.id = "taskwindow-glow";
    glow.setAttribute("aria-hidden", "true");
    glow.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483646;opacity:0;transition:opacity 300ms;";
    glow.innerHTML = `<div id="taskwindow-glow-inner" style="position:absolute;inset:0;box-shadow: inset 0 0 25px rgba(37,99,235,.5), inset 0 0 60px rgba(37,99,235,.25);"></div>`;
    document.documentElement.appendChild(glow);

    const host = document.createElement("div");
    host.id = "taskwindow-cursor";
    host.setAttribute("aria-hidden", "true");
    host.style.cssText = "all:initial;position:fixed;top:0;left:0;width:0;height:0;pointer-events:none;z-index:2147483646;";
    const root = host.attachShadow({ mode: "closed" });
    root.innerHTML = `<style>${SHADOW_CSS}</style><div class="cursor">${CURSOR_SVG}</div>`;
    cursor = root.querySelector(".cursor");
    document.documentElement.appendChild(host);
  }

  function showGlow() {
    glow.style.opacity = "1";
  }

  // Marks a click on the cursor itself: the arrow dips. The page shows the
  // rest: the real pointer is on the element, so its own :hover and :active
  // styles apply, as they would for a person's click.
  function press() {
    if (reduced) return;
    cursor.classList.add("press");
    setTimeout(() => cursor.classList.remove("press"), 110);
  }

  // `step` is set when the move is one point of a path the real pointer is
  // walking (tools/human.js): follow it linearly over that many ms, so the
  // path's own pacing shows through. Otherwise glide there on our own.
  function moveTo(x, y, click, step) {
    ensure();
    // Longer hops take longer, so they read as a glide rather than a jump;
    // capped so a screenshot straight after the action finds the cursor there.
    // The first appearance places it without a glide in from off-screen.
    const d = visible ? Math.hypot(x - at.x, y - at.y) : 0;
    const glide = step ?? Math.round(Math.min(300, 90 + 70 * Math.log2(1 + d / 16)));
    const ms = visible ? glide : 0;
    const easing = step == null ? "cubic-bezier(.3,0,.2,1)" : "linear";
    cursor.style.transition = `transform ${ms}ms ${easing}, opacity 200ms`;
    cursor.style.transform = `translate3d(${x - TIP}px, ${y - TIP}px, 0)`;
    cursor.style.opacity = "1";
    visible = true;
    at = { x, y };
    if (click) setTimeout(press, ms);
    showGlow();
  }

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg?.type !== "taskwindow:indicator") return;
    try {
      if (msg.op === "move" && typeof msg.x === "number" && typeof msg.y === "number") {
        const step = Number.isFinite(msg.ms) ? Math.min(Math.max(msg.ms, 0), 1000) : undefined;
        moveTo(msg.x, msg.y, msg.click === true, step);
      } else if (msg.op === "press" && typeof msg.x === "number" && typeof msg.y === "number") {
        // The real button just went down here: dip now, not after a glide.
        if (!visible) moveTo(msg.x, msg.y, false);
        press();
      } else if (msg.op === "focus") {
        ensure();
        showGlow();
      } else if (msg.op === "hide") {
        if (glow) glow.style.opacity = "0";
        if (cursor) cursor.style.opacity = "0";
        visible = false;
      }
    } catch {}
    // no async response; let the ax-tree listener own the response channel
  });
})();
