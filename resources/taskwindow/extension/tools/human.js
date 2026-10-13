/**
 * Wonder: human-shaped input.
 *
 * Synthetic input is not just recognisable, it is often *ineffective*. A single
 * mouseWheel carrying 1500px never fires the intermediate scroll positions that
 * IntersectionObserver, infinite scroll and lazy images wait for; a teleport
 * straight to press() never fires the mouseenter a hover-menu needs;
 * Input.insertText fires no key events at all, so type-ahead boxes stay empty.
 * Wonder fixes those by moving the way a hand does, which happens to be the
 * same fix in both directions.
 *
 * The models are the standard ones:
 *   - Fitts's Law for how long a move to a target of a given size takes;
 *   - Flash & Hogan's minimum-jerk profile for the bell-shaped velocity inside
 *     one stroke (this is what a plain Bezier ease does not give you);
 *   - the two-component model of aimed movement for overshoot-then-correct;
 *   - lognormal inter-key intervals, with the pauses that fall at word and
 *     sentence boundaries.
 *
 * Non-goal: defeating a bot detector. The Bezier-curve generators are the
 * labelled bot class in the mouse-dynamics literature (see BeCAPTCHA-Mouse,
 * DMTG), so treat the resemblance as a side effect, never as a guarantee.
 */

import { send } from "./cdp.js";
import { usKey } from "./keys.js";

/** Below these, humanising is noise: a hand nudges a wheel and flicks a cursor too. */
export const MOVE_FLOOR_PX = 48;
export const SCROLL_FLOOR_PX = 100;
/** Past this, a human reaches for the clipboard rather than typing it out. */
export const PASTE_THRESHOLD = 200;
/** Typing is capped so a long string cannot run into the tool timeout. */
const TYPE_BUDGET_MS = 7000;

const rand = (lo, hi) => lo + Math.random() * (hi - lo);
const randInt = (lo, hi) => Math.round(rand(lo, hi));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const round2 = (v) => Math.round(v * 100) / 100;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Standard normal via Box-Muller; callers clamp. */
function gauss(mean, sd) {
  let u = 0;
  while (u === 0) u = Math.random();
  let v = 0;
  while (v === 0) v = Math.random();
  return mean + sd * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/* ------------------------------------------------------------------ cursor */

/** tabId -> last dispatched cursor position, so a path starts where the last one ended. */
const cursors = new Map();

export function noteCursor(tabId, x, y) {
  if (Number.isFinite(x) && Number.isFinite(y)) cursors.set(tabId, { x, y });
}

export function dropCursor(tabId) {
  cursors.delete(tabId);
}

/**
 * Where the pointer is. Unknown tabs get a random point in the middle of the
 * viewport rather than (0,0): every path from a fixed origin shares one angle,
 * which is the single loudest tell in the trajectory literature.
 */
export function cursorOf(tabId, viewport) {
  const known = cursors.get(tabId);
  if (known) return known;
  const w = viewport?.width || 1280;
  const h = viewport?.height || 800;
  const seeded = { x: round2(rand(w * 0.25, w * 0.75)), y: round2(rand(h * 0.3, h * 0.8)) };
  cursors.set(tabId, seeded);
  return seeded;
}

/**
 * Mirror a pointer event onto the drawn cursor (content/indicator.js), so it
 * rides the same path at the same pace as the events the page receives, and
 * its press dip lands with the real press. Fire-and-forget: the cursor is
 * decoration and must never slow or fail the input it follows.
 */
function show(tabId, payload) {
  try {
    chrome.tabs.sendMessage(tabId, { type: "taskwindow:indicator", ...payload }).catch(() => {});
  } catch {}
}

/** A real mouseMoved, mirrored. `ms` is how long the drawn cursor takes to follow. */
async function pointTo(tabId, x, y, ms) {
  await send(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y });
  show(tabId, { op: "move", x, y, ms });
}

/* -------------------------------------------------------------------- path */

/** Fitts's Law movement time in ms; a/b are the classic mouse regression constants. */
function fittsMs(distance, targetWidth) {
  const index = Math.log2((2 * Math.max(distance, 1)) / Math.max(targetWidth, 1));
  return clamp(rand(0.8, 1.25) * (60 + 130 * Math.max(index, 0)), 80, 1400);
}

/** Flash & Hogan minimum-jerk position profile: velocity is a bell, zero at both ends. */
const minJerk = (t) => t * t * t * (10 + t * (-15 + 6 * t));

function bezier(p0, p1, p2, p3, t) {
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const d = t * t * t;
  return {
    x: a * p0.x + b * p1.x + c * p2.x + d * p3.x,
    y: a * p0.y + b * p1.y + c * p2.y + d * p3.y,
  };
}

/**
 * A pointer path from `from` to `to` as {x, y, dt} steps. The last point is
 * exactly `to`: all the jitter lives in the path, never in the destination,
 * so a humanised click lands where the caller asked and nowhere else.
 */
export function movePath(from, to, { targetWidth = 24 } = {}) {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const distance = Math.hypot(dx, dy);
  const ms = fittsMs(distance, targetWidth);
  const steps = clamp(Math.round(ms / 16), 4, 30); // ~60Hz, capped so a long move is not 100 round trips

  // Bow the curve to one side only. Picking a side per control point makes an
  // S-curve, which no hand draws.
  const side = Math.random() < 0.5 ? 1 : -1;
  const bow = side * distance * rand(0.04, 0.16);
  const nx = distance ? -dy / distance : 0;
  const ny = distance ? dx / distance : 0;
  const c1 = { x: from.x + dx * 0.3 + nx * bow * rand(0.6, 1), y: from.y + dy * 0.3 + ny * bow * rand(0.6, 1) };
  const c2 = { x: from.x + dx * 0.7 + nx * bow * rand(0.6, 1), y: from.y + dy * 0.7 + ny * bow * rand(0.6, 1) };

  // Two-component model: the ballistic phase overshoots a far, small target and
  // a corrective phase pulls back. A close or large target is caught first time.
  const index = Math.log2((2 * Math.max(distance, 1)) / Math.max(targetWidth, 1));
  const overshoots = index > 3.5 && Math.random() < 0.65;
  const aim = overshoots
    ? { x: to.x + dx * rand(0.02, 0.07), y: to.y + dy * rand(0.02, 0.07) }
    : to;

  const path = [];
  for (let i = 1; i <= steps; i++) {
    const p = bezier(from, c1, c2, aim, minJerk(i / steps));
    path.push({ x: round2(p.x), y: round2(p.y), dt: round2(ms / steps) });
  }
  if (overshoots) {
    const back = randInt(2, 4);
    const start = path[path.length - 1];
    for (let i = 1; i <= back; i++) {
      const t = minJerk(i / back);
      path.push({
        x: round2(start.x + (to.x - start.x) * t),
        y: round2(start.y + (to.y - start.y) * t),
        dt: round2(rand(18, 34)),
      });
    }
  }
  path[path.length - 1] = { ...path[path.length - 1], x: to.x, y: to.y };
  return path;
}

/* ------------------------------------------------------------------ scroll */

/**
 * One requested scroll as several uneven bursts. A wheel is not a metronome:
 * the deltas differ, the speeds differ, and the pauses between them differ.
 * The deltas always sum to exactly (dx, dy) so the caller's arithmetic holds.
 */
export function scrollPlan(dx, dy) {
  const total = Math.hypot(dx, dy);
  const bursts = total < 400 ? 1 : total < 1200 ? randInt(2, 3) : randInt(3, 4);
  const weights = Array.from({ length: bursts }, () => rand(0.6, 1.6));
  const sum = weights.reduce((a, b) => a + b, 0);
  let spentX = 0;
  let spentY = 0;
  return weights.map((w, i) => {
    const last = i === bursts - 1;
    // The last burst takes the remainder, so rounding never loses a pixel.
    const bx = last ? dx - spentX : round2((dx * w) / sum);
    const by = last ? dy - spentY : round2((dy * w) / sum);
    spentX += bx;
    spentY += by;
    return {
      dx: bx,
      dy: by,
      speed: randInt(650, 1150),
      pauseMs: last ? randInt(90, 260) : randInt(60, 190),
    };
  });
}

/* -------------------------------------------------------------------- type */

/** A printable character as a key event. Only what dispatchKeyEvent needs to insert it. */
function charKey(ch) {
  if (ch === "\n" || ch === "\r") {
    return { key: "Enter", code: "Enter", vk: 13, text: "\r" };
  }
  const { code, vk, shift } = usKey(ch);
  return { key: ch, code, vk, modifiers: shift ? 8 : 0, text: ch };
}

/**
 * Per-character key events with human inter-key intervals, or a paste for text
 * long enough that a human would have pasted it too. Intervals are lognormal
 * around ~105ms, with the longer pauses that fall after sentence punctuation
 * and at the occasional mid-thought stall, then scaled to fit the budget.
 */
export function typePlan(text, { budgetMs = TYPE_BUDGET_MS } = {}) {
  if (text.length > PASTE_THRESHOLD) return { mode: "paste" };

  // By code point, so an emoji or other astral character is one key, not two
  // lone surrogates that each arrive as U+FFFD.
  const chars = Array.from(text);
  const keys = [];
  let sinceStall = 0;
  for (let i = 0; i < chars.length; i++) {
    const ch = chars[i];
    const prev = chars[i - 1];
    let delay = clamp(Math.exp(gauss(Math.log(105), 0.32)), 45, 320);
    if (prev === "." || prev === "?" || prev === "!") delay += rand(180, 420);
    else if (prev === "," || prev === ";" || prev === ":") delay += rand(70, 180);
    else if (prev === " ") delay += rand(0, 45);
    if (++sinceStall > randInt(12, 26)) {
      delay += rand(150, 400);
      sinceStall = 0;
    }
    keys.push({ ch, delayMs: round2(delay), hold: round2(rand(38, 92)) });
  }

  const total = keys.reduce((a, k) => a + k.delayMs + k.hold, 0);
  if (total > budgetMs) {
    const scale = budgetMs / total;
    for (const k of keys) {
      k.delayMs = round2(k.delayMs * scale);
      k.hold = round2(k.hold * scale);
    }
  }
  return { mode: "keys", keys };
}

/* ------------------------------------------------------------------ idle */

/**
 * Aimless cursor drift: a few short strokes to nearby points and a settle,
 * the way a hand rests on a mouse while its owner reads.
 */
export function wanderPlan(from, viewport) {
  const w = viewport?.width || 1280;
  const h = viewport?.height || 800;
  const stops = randInt(2, 4);
  const out = [];
  let at = from;
  for (let i = 0; i < stops; i++) {
    const to = {
      x: round2(clamp(at.x + gauss(0, 120), 8, w - 8)),
      y: round2(clamp(at.y + gauss(0, 90), 8, h - 8)),
    };
    out.push({ to, pauseMs: randInt(120, 520) });
    at = to;
  }
  return out;
}

/* ------------------------------------------------------- CDP dispatch ---- */

async function viewportOf(tabId) {
  try {
    const metrics = await send(tabId, "Page.getLayoutMetrics");
    const vp = metrics?.cssLayoutViewport || metrics?.layoutViewport || {};
    return { width: vp.clientWidth || 1280, height: vp.clientHeight || 800 };
  } catch {
    return { width: 1280, height: 800 };
  }
}

/** Walk the pointer to (x, y) along a human path. Returns the number of moves sent. */
export async function glideTo(tabId, x, y, { targetWidth = 24, viewport } = {}) {
  const from = cursorOf(tabId, viewport || (await viewportOf(tabId)));
  if (Math.hypot(x - from.x, y - from.y) < MOVE_FLOOR_PX) {
    await pointTo(tabId, x, y);
    noteCursor(tabId, x, y);
    return 1;
  }
  const path = movePath(from, { x, y }, { targetWidth });
  for (const step of path) {
    await pointTo(tabId, step.x, step.y, step.dt);
    if (step.dt > 4) await sleep(step.dt);
  }
  noteCursor(tabId, x, y);
  return path.length;
}

/* --------------------------------------------------------------- aim ---- */

/**
 * Where on a control the press lands. A hand does not hit the exact centre, so
 * the point moves off the requested one, but only a little: agents check a
 * click by screenshot, and the cursor there must still sit plainly on the
 * control they meant. At most 20% of the control's size, capped at 6px across
 * and 3px down, mostly much less, and never within 2px of the control's edge.
 */
export function aimOffset(x, y, rect) {
  const capX = Math.min(0.2 * rect.width, 6);
  const capY = Math.min(0.2 * rect.height, 3);
  const at = {
    x: Math.round(x + clamp(gauss(0, capX / 2), -capX, capX)),
    y: Math.round(y + clamp(gauss(0, capY / 2), -capY, capY)),
  };
  const inside =
    at.x >= rect.left + 2 && at.x <= rect.left + rect.width - 2 &&
    at.y >= rect.top + 2 && at.y <= rect.top + rect.height - 2;
  return inside ? at : { x, y };
}

/**
 * Runs in the tab (the extension's isolated world, so page scripts cannot
 * patch what it calls). With one point: the box of the control under it, or
 * null where the pixel itself is the point — no control, a frame, a canvas or
 * video, a slider, a text box that already holds text (the click places the
 * caret). With a second point: whether that one still lands on the same control.
 */
export function pageAim(x, y, nx, ny) {
  const CONTROL =
    "button, a[href], label, select, summary, textarea, input, [role=button], [role=link], [role=checkbox], " +
    "[role=radio], [role=switch], [role=tab], [role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], [role=option]";
  const deepHit = (px, py) => {
    let el = document.elementFromPoint(px, py);
    while (el?.shadowRoot) {
      const inner = el.shadowRoot.elementFromPoint(px, py);
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  };
  const controlOf = (el) => {
    for (let n = el; n; n = n.parentElement || n.getRootNode().host) {
      if (n.nodeType === 1 && n.matches(CONTROL)) return n;
    }
    return null;
  };
  const hit = deepHit(x, y);
  if (!hit || /^(IFRAME|FRAME|CANVAS|VIDEO|EMBED|OBJECT)$/.test(hit.tagName)) return null;
  const control = controlOf(hit);
  if (!control || control.isContentEditable || control.matches("input[type=range]")) return null;
  const textBox = control.matches(
    "textarea, input:not([type]), input[type=text], input[type=search], input[type=email], input[type=url], " +
      "input[type=tel], input[type=password], input[type=number]"
  );
  if (textBox && control.value !== "") return null;
  if (nx == null) {
    const r = control.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  }
  const again = deepHit(nx, ny);
  return !!again && controlOf(again) === control;
}

/** The press point for a click at (x, y): slightly off it on the same control, or exactly it. */
async function aimAt(tabId, x, y) {
  const probe = (args) =>
    chrome.scripting.executeScript({ target: { tabId }, func: pageAim, args }).then((r) => r?.[0]?.result);
  try {
    const rect = await probe([x, y, null, null]);
    if (!rect || ![rect.left, rect.top, rect.width, rect.height].every(Number.isFinite)) return { x, y };
    const at = aimOffset(x, y, rect);
    if (at.x === x && at.y === y) return at;
    return (await probe([x, y, at.x, at.y])) === true ? at : { x, y };
  } catch {
    return { x, y }; // a page we cannot script (chrome://, the store): the requested pixel
  }
}

/**
 * Approach, settle, dwell, then press and release with a human hold. The press
 * lands a little off (x, y) but on the same control (see aimAt); returns the
 * number of moves and where it pressed.
 */
export async function humanClick(tabId, { x: askedX, y: askedY, button, buttons, clicks, targetWidth }) {
  const { x, y } = await aimAt(tabId, askedX, askedY);
  const moves = await glideTo(tabId, x, y, { targetWidth });

  // The hand is never perfectly still on the target before it commits.
  for (let i = 0; i < randInt(1, 2); i++) {
    const dwell = randInt(12, 30);
    await pointTo(tabId, round2(x + rand(-1.2, 1.2)), round2(y + rand(-1.2, 1.2)), dwell);
    await sleep(dwell);
  }
  await pointTo(tabId, x, y, 20);
  await sleep(randInt(60, 180)); // target acquired, decision made

  for (let i = 1; i <= clicks; i++) {
    await send(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button, buttons, clickCount: i });
    show(tabId, { op: "press", x, y });
    await sleep(randInt(55, 110));
    await send(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button, buttons: 0, clickCount: i });
    // Stay well inside the double-click threshold, or the OS sees separate clicks.
    if (i < clicks) await sleep(randInt(70, 140));
  }
  return { moves, x, y };
}

/**
 * A wheel scroll as a real gesture rather than one jumbo delta.
 *
 * Input.synthesizeScrollGesture is Chrome's own smooth-scroll synthesiser, so
 * the page sees the whole tick sequence its observers are waiting for. Its
 * sign convention is inverted relative to dispatchMouseEvent's deltas:
 * positive yDistance scrolls *up*, positive xDistance scrolls *left*.
 * If the command is unavailable, fall back to a hand-rolled tick loop.
 */
export async function humanScroll(tabId, { x, y, dx, dy }) {
  const plan = scrollPlan(dx, dy);
  for (const burst of plan) {
    try {
      await send(tabId, "Input.synthesizeScrollGesture", {
        x,
        y,
        xDistance: -burst.dx,
        yDistance: -burst.dy,
        speed: burst.speed,
        preventFling: true,
        gestureSourceType: "mouse",
      });
    } catch {
      await wheelTicks(tabId, x, y, burst.dx, burst.dy);
    }
    await sleep(burst.pauseMs);
  }
  noteCursor(tabId, x, y);
  return plan.length;
}

/** Fallback for Chromium builds without the experimental gesture command. */
async function wheelTicks(tabId, x, y, dx, dy) {
  const ticks = clamp(Math.round(Math.hypot(dx, dy) / 100), 1, 24);
  for (let i = 1; i <= ticks; i++) {
    const t = minJerk(i / ticks) - minJerk((i - 1) / ticks);
    await send(tabId, "Input.dispatchMouseEvent", {
      type: "mouseWheel",
      x,
      y,
      deltaX: round2(dx * t),
      deltaY: round2(dy * t),
    });
    await sleep(randInt(12, 26));
  }
}

/** Real keydown/keyup per character, so type-ahead and key handlers actually fire. */
export async function humanType(tabId, text, { budgetMs = TYPE_BUDGET_MS } = {}) {
  const plan = typePlan(text, { budgetMs });
  if (plan.mode === "paste") {
    await send(tabId, "Input.insertText", { text });
    return { mode: "paste", keys: 0 };
  }
  // The plan fits the budget on paper, but every key also spends two CDP round
  // trips (~60ms measured). Squeeze what is left of the plan into what is left
  // of the budget, so the overhead comes out of the pauses, not on top of them.
  const start = Date.now();
  let planLeft = plan.keys.reduce((a, k) => a + k.delayMs + k.hold, 0);
  for (const k of plan.keys) {
    const scale = clamp((budgetMs - (Date.now() - start)) / Math.max(planLeft, 1), 0, 1);
    planLeft -= k.delayMs + k.hold;
    await sleep(k.delayMs * scale);
    const spec = charKey(k.ch);
    const base = {
      key: spec.key,
      code: spec.code,
      windowsVirtualKeyCode: spec.vk,
      nativeVirtualKeyCode: spec.vk,
      modifiers: spec.modifiers,
    };
    await send(tabId, "Input.dispatchKeyEvent", { type: "keyDown", ...base, text: spec.text });
    await sleep(k.hold * scale);
    await send(tabId, "Input.dispatchKeyEvent", { type: "keyUp", ...base });
  }
  return { mode: "keys", keys: plan.keys.length };
}

/**
 * Filler between steps: cursor drift and a settle. With `jiggle` it also rocks
 * the wheel by that many pixels and puts it back, so the scroll position ends
 * exactly where it started (the page still sees the scroll events, which on a
 * scroll-reactive page can shift layout — that is why it is opt-in).
 */
export async function humanIdle(tabId, { jiggle = 0 } = {}) {
  const viewport = await viewportOf(tabId);
  const from = cursorOf(tabId, viewport);
  const stops = wanderPlan(from, viewport);
  for (const stop of stops) {
    await glideTo(tabId, stop.to.x, stop.to.y, { targetWidth: 60, viewport });
    await sleep(stop.pauseMs);
  }
  if (jiggle > 0) {
    const at = cursorOf(tabId, viewport);
    await humanScroll(tabId, { x: at.x, y: at.y, dx: 0, dy: jiggle });
    await sleep(randInt(120, 400));
    await humanScroll(tabId, { x: at.x, y: at.y, dx: 0, dy: -jiggle });
  }
  return stops.length;
}
