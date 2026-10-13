import { resolveTab } from "./tabs.js";
import { indicator } from "./page.js";
import { withDebugger, send } from "./cdp.js";
import {
  humanClick, humanScroll, humanType, humanIdle, glideTo, noteCursor,
  SCROLL_FLOOR_PX, PASTE_THRESHOLD,
} from "./human.js";
import { usKey, charOfCode } from "./keys.js";

const KEY_CODES = {
  enter: [13, "Enter"], tab: [9, "Tab"], escape: [27, "Escape"], esc: [27, "Escape"],
  backspace: [8, "Backspace"], delete: [46, "Delete"], del: [46, "Delete"],
  arrowleft: [37, "ArrowLeft"], left: [37, "ArrowLeft"],
  arrowup: [38, "ArrowUp"], up: [38, "ArrowUp"],
  arrowright: [39, "ArrowRight"], right: [39, "ArrowRight"],
  arrowdown: [40, "ArrowDown"], down: [40, "ArrowDown"],
  home: [36, "Home"], end: [35, "End"], pageup: [33, "PageUp"], pagedown: [34, "PageDown"],
  " ": [32, "Space"], space: [32, "Space"],
};

const BUTTONS = { left: ["left", 1], right: ["right", 2], middle: ["middle", 4] };

function keySpec(name) {
  const parts = String(name).split("+");
  let modifiers = 0;
  for (let i = 0; i < parts.length - 1; i++) {
    const m = parts[i].toLowerCase();
    if (m === "alt") modifiers |= 1;
    else if (m === "ctrl" || m === "control") modifiers |= 2;
    else if (m === "meta" || m === "cmd" || m === "command") modifiers |= 4;
    else if (m === "shift") modifiers |= 8;
    else throw new Error(`unknown key modifier "${parts[i]}" in "${name}"`);
  }
  // A key named by its code ("Period", "KeyA", "Digit3") is that key's character.
  const keyName = charOfCode(parts[parts.length - 1]) ?? parts[parts.length - 1];
  let vk, code, key = keyName;
  const lower = keyName.toLowerCase();
  if (KEY_CODES[lower] !== undefined || KEY_CODES[keyName] !== undefined) {
    [vk, code] = KEY_CODES[lower] ?? KEY_CODES[keyName];
    if (code === "Space") key = " ";
  } else if (/^f([1-9]|1[0-2])$/.test(lower)) {
    const n = Number(lower.slice(1));
    vk = 111 + n;
    code = "F" + n;
    key = code;
  } else if (keyName.length === 1) {
    const us = usKey(keyName);
    vk = us.vk;
    code = us.code || keyName;
    if (us.shift) modifiers |= 8;
  } else {
    // Unknown named key: send it raw and let the page decide.
    vk = 0;
    code = keyName;
  }
  return { key, code, windowsVirtualKeyCode: vk, modifiers, text: keyName.length === 1 ? keyName : undefined };
}

/** Page zoom and HiDPI both fold into window.devicePixelRatio; 1 when unreadable. */
async function devicePixelRatio(tabId) {
  try {
    const { result } = await send(tabId, "Runtime.evaluate", {
      expression: "window.devicePixelRatio",
      returnByValue: true,
    });
    const dpr = Number(result?.value);
    return Number.isFinite(dpr) && dpr > 0 ? dpr : 1;
  } catch {
    return 1;
  }
}

/**
 * Runs in the tab (the extension's isolated world, whose globals persist
 * between injections and are invisible to the page). "before" records the
 * scroll chain under (x, y) — the scrollable ancestors a wheel there moves,
 * innermost first, then the page — and returns its length, or null when the
 * point is over a frame, whose scrolling this frame cannot see. "read" returns
 * how far that chain has moved since, and whether every part of it is at its
 * end in the direction asked. No timers here: a background tab throttles them.
 */
export function pageScroll(x, y, dx, dy, phase) {
  if (phase === "before") {
    let el = document.elementFromPoint(x, y);
    while (el?.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === el) break;
      el = inner;
    }
    if (el && /^(IFRAME|FRAME)$/.test(el.tagName)) return null;
    const page = document.scrollingElement || document.documentElement;
    const chain = [];
    for (let n = el; n; n = n.parentElement || n.getRootNode().host) {
      if (n === page || n === document.body || n === document.documentElement) continue;
      const cs = getComputedStyle(n);
      const canY = dy && /auto|scroll|overlay/.test(cs.overflowY) && n.scrollHeight > n.clientHeight;
      const canX = dx && /auto|scroll|overlay/.test(cs.overflowX) && n.scrollWidth > n.clientWidth;
      if (canX || canY) chain.push(n);
    }
    chain.push(page);
    globalThis.__taskwindowScroll = chain.map((n) => ({ ref: new WeakRef(n), left: n.scrollLeft, top: n.scrollTop }));
    return chain.length;
  }
  const chain = globalThis.__taskwindowScroll;
  if (!chain) return null;
  let movedX = 0;
  let movedY = 0;
  let atEnd = true;
  for (const c of chain) {
    const n = c.ref.deref();
    if (!n) continue;
    movedX += n.scrollLeft - c.left;
    movedY += n.scrollTop - c.top;
    const endY = dy > 0 ? n.scrollTop >= n.scrollHeight - n.clientHeight - 1 : dy < 0 ? n.scrollTop <= 0 : true;
    const endX = dx > 0 ? n.scrollLeft >= n.scrollWidth - n.clientWidth - 1 : dx < 0 ? n.scrollLeft <= 0 : true;
    if (!endX || !endY) atEnd = false;
  }
  return { dx: Math.round(movedX), dy: Math.round(movedY), atEnd };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function scrollProbe(tabId, args) {
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId }, func: pageScroll, args });
    return res?.result;
  } catch {
    return null; // a page we cannot script (chrome://, the store)
  }
}

/**
 * How far the content under (x, y) actually moved, once it stops moving: a
 * wheel scroll may still be animating when its event returns. Polled from
 * here, not from the page, so a throttled background tab cannot stall it.
 * Null when the page cannot say.
 */
async function scrolledBy(tabId, args) {
  const start = Date.now();
  let last = null;
  let stableSince = start;
  for (;;) {
    const now = await scrollProbe(tabId, [...args, "read"]);
    if (!now || !Number.isFinite(now.dx) || !Number.isFinite(now.dy)) return last;
    if (!last || now.dx !== last.dx || now.dy !== last.dy) {
      last = now;
      stableSince = Date.now();
    } else if (Date.now() - stableSince >= 150) {
      return now;
    }
    if (Date.now() - start > 1500) return now;
    await sleep(40);
  }
}

/**
 * The scroll result says what happened, not what was asked: an infinite list
 * whose end arrives before the next page loads, or a scroll area with less
 * room than asked, moves less, and the agent should know to scroll again.
 */
function scrollReport({ dx, dy, x, y, moved, how }) {
  const at = `at (${x}, ${y})${how}`;
  if (!moved || (Math.abs(moved.dx - dx) <= 2 && Math.abs(moved.dy - dy) <= 2)) return `scrolled (${dx}, ${dy}) ${at}`;
  const why = moved.atEnd
    ? "reached the end of what scrolls there (a list that loads more may grow: scroll again to continue)"
    : "the content stopped short of the distance asked";
  return `scrolled (${moved.dx}, ${moved.dy}) of the (${dx}, ${dy}) asked ${at}: ${why}`;
}

function readPngDimensions(b64) {
  try {
    // PNG IHDR: width/height are big-endian uint32 at offsets 16 and 20.
    const bin = atob(b64.slice(0, 40));
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    const dv = new DataView(u8.buffer);
    return { width: dv.getUint32(16), height: dv.getUint32(20) };
  } catch {
    return null;
  }
}

export async function computer(params) {
  const { action } = params;
  const tab = await resolveTab(params.tabId, params.sessionToken);

  // A humanised pointer walks a path over time, and human.js mirrors each step
  // onto the drawn cursor. Jumping it to the target here would put it there
  // before the real pointer, so only inject the indicator, and wait for it so
  // the first steps have a listener.
  const walks = action === "idle" || (params.human && (action.endsWith("_click") || action === "mouse_move"));
  if (walks) {
    await indicator(tab.id, { op: "focus" });
  } else if (action !== "screenshot" && action !== "wait" && action !== "idle") {
    // Visual "an agent is acting here" feedback, best-effort.
    indicator(tab.id, {
      op: action === "type" || action === "key" ? "focus" : "move",
      x: params.x,
      y: params.y,
      click: action.endsWith("_click"),
    });
  }

  if (action === "screenshot") {
    return withDebugger(tab.id, async (tabId) => {
      // Input.dispatchMouseEvent takes CSS pixels, but Page.captureScreenshot
      // renders device pixels: on a Retina display (or a zoomed page) the raw
      // image is 2x the coordinate space, so a click read off it lands ~2x too
      // far right and down — off the button, or off the viewport. Clip at
      // 1/devicePixelRatio so one image pixel is one coordinate unit. The clip
      // is document-relative (as Playwright's takeScreenshot does it), so a
      // viewport shot starts at the current scroll offset, not the page top.
      const { cssLayoutViewport: layout, cssVisualViewport: visual, cssContentSize: content } =
        await send(tabId, "Page.getLayoutMetrics");
      const dpr = await devicePixelRatio(tabId);
      const clip = params.fullPage
        ? { x: 0, y: 0, width: content.width, height: content.height }
        : { x: visual.pageX, y: visual.pageY, width: layout.clientWidth, height: layout.clientHeight };
      clip.width = Math.max(1, Math.round(clip.width));
      clip.height = Math.max(1, Math.round(clip.height));
      const shot = await send(tabId, "Page.captureScreenshot", {
        format: "png",
        captureBeyondViewport: !!params.fullPage,
        clip: { ...clip, scale: 1 / dpr },
      });
      const dims = readPngDimensions(shot.data);
      return {
        image: { data: shot.data, mimeType: "image/png" },
        text: `screenshot of tab ${tabId}${dims ? ` (${dims.width}x${dims.height} CSS px — 1 image px = 1 coordinate unit)` : ""}${params.fullPage ? " (full page)" : " (viewport)"}`,
      };
    });
  }

  if (action === "wait") {
    const ms = params.ms ?? 1000;
    await new Promise((r) => setTimeout(r, ms));
    return { text: `waited ${ms}ms` };
  }

  // Input goes to the tab as it is, hidden or not: CDP delivers mouse, wheel
  // and key events to a background tab (verified in Chrome, see README), so
  // nothing here ever changes which tab a window shows.
  return withDebugger(tab.id, async (tabId) => {
    switch (action) {
      case "left_click":
      case "right_click":
      case "middle_click":
      case "double_click":
      case "triple_click": {
        const x = params.x ?? 0;
        const y = params.y ?? 0;
        const [button, btnBits] = BUTTONS[action === "left_click" ? "left" : action === "right_click" ? "right" : "middle"];
        const clicks = action === "double_click" ? 2 : action === "triple_click" ? 3 : 1;
        if (params.human) {
          const hit = await humanClick(tabId, { x, y, button, buttons: btnBits, clicks, targetWidth: params.targetWidth });
          const off = hit.x !== x || hit.y !== y ? `, pressed at (${hit.x}, ${hit.y}) on the same element` : "";
          return { text: `${action} at (${x}, ${y}) in tab ${tabId} (human: approached over ${hit.moves} point${hit.moves === 1 ? "" : "s"}${off})` };
        }
        await send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
        for (let i = 1; i <= clicks; i++) {
          await send(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button, buttons: btnBits, clickCount: i });
          await send(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button, buttons: 0, clickCount: i });
        }
        noteCursor(tabId, x, y);
        return { text: `${action} at (${x}, ${y}) in tab ${tabId}` };
      }
      case "type": {
        if (!params.text) throw new Error('computer "type" requires text');
        if (params.human) {
          const typed = await humanType(tabId, params.text);
          return {
            text: typed.mode === "paste"
              ? `pasted ${params.text.length} chars into tab ${tabId} (over ${PASTE_THRESHOLD} chars, so typed the way a human would: as a paste)`
              : `typed ${typed.keys} chars into tab ${tabId} as real key events`,
          };
        }
        await send(tabId, "Input.insertText", { text: params.text });
        return { text: `typed ${params.text.length} chars into tab ${tabId}` };
      }
      case "key": {
        if (!params.key) throw new Error('computer "key" requires key (e.g. "Enter", "Control+a")');
        const spec = keySpec(params.key);
        const base = {
          key: spec.key, code: spec.code,
          windowsVirtualKeyCode: spec.windowsVirtualKeyCode, nativeVirtualKeyCode: spec.windowsVirtualKeyCode,
          modifiers: spec.modifiers,
        };
        await send(tabId, "Input.dispatchKeyEvent", { type: spec.text ? "keyDown" : "rawKeyDown", ...base, text: spec.text });
        await send(tabId, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
        return { text: `pressed ${params.key}` };
      }
      case "scroll": {
        const dx = params.dx ?? 0;
        const dy = params.dy ?? 0;
        let x = params.x, y = params.y;
        if (x == null || y == null) {
          const metrics = await send(tabId, "Page.getLayoutMetrics");
          const vp = metrics.cssLayoutViewport || metrics.layoutViewport || {};
          x = x ?? Math.floor((vp.clientWidth || 800) / 2);
          y = y ?? Math.floor((vp.clientHeight || 600) / 2);
        }
        const probe = [x, y, dx, dy];
        const watched = Number.isFinite(await scrollProbe(tabId, [...probe, "before"]));
        let how = "";
        if (params.human && Math.hypot(dx, dy) >= SCROLL_FLOOR_PX) {
          const bursts = await humanScroll(tabId, { x, y, dx, dy });
          how = ` in ${bursts} human burst${bursts === 1 ? "" : "s"}`;
        } else {
          await send(tabId, "Input.dispatchMouseEvent", { type: "mouseWheel", x, y, deltaX: dx, deltaY: dy });
          noteCursor(tabId, x, y);
        }
        const moved = watched ? await scrolledBy(tabId, probe) : null;
        return { text: scrollReport({ dx, dy, x, y, moved, how }) };
      }
      case "mouse_move": {
        const x = params.x ?? 0;
        const y = params.y ?? 0;
        if (params.human) {
          const moves = await glideTo(tabId, x, y, { targetWidth: params.targetWidth });
          return { text: `mouse moved to (${x}, ${y}) along ${moves} point${moves === 1 ? "" : "s"}` };
        }
        await send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
        noteCursor(tabId, x, y);
        return { text: `mouse moved to (${x}, ${y})` };
      }
      case "idle": {
        const jiggle = params.dy ?? 0;
        const stops = await humanIdle(tabId, { jiggle });
        return {
          text: `idled: cursor drifted through ${stops} stops${jiggle ? ` plus a ${jiggle}px scroll jiggle (returned to the same offset)` : ""}`,
        };
      }
      default:
        throw new Error(`unknown computer action "${action}"`);
    }
  });
}
