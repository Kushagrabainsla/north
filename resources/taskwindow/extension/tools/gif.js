import { resolveTab } from "./tabs.js";
import { withDebugger, send } from "./cdp.js";

const OFFSCREEN_URL = "offscreen/offscreen.html";

async function ensureOffscreen() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
  });
  if (contexts.length > 0) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: ["DISPLAY_MEDIA"],
    justification: "Encoding recorded browser frames into an animated GIF",
  });
}

async function offscreenMessage(msg, attempt = 0) {
  await ensureOffscreen();
  try {
    return await chrome.runtime.sendMessage({ target: "offscreen", ...msg });
  } catch (err) {
    // The offscreen document may still be loading right after creation.
    if (attempt < 5 && /Receiving end does not exist/i.test(err?.message || "")) {
      await new Promise((r) => setTimeout(r, 150));
      return offscreenMessage(msg, attempt + 1);
    }
    throw err;
  }
}

export async function gifRecord({ tabId, sessionToken, duration, fps = 5, maxWidth = 800 }) {
  const tab = await resolveTab(tabId, sessionToken);
  const interval = 1000 / fps;
  const maxFrames = 200;

  await offscreenMessage({ op: "begin", maxWidth });

  const started = Date.now();
  let frames = 0;
  try {
    while (frames < maxFrames) {
      const elapsed = Date.now() - started;
      if (elapsed >= duration * 1000) break;
      await withDebugger(tab.id, async (tid) => {
        const shot = await send(tid, "Page.captureScreenshot", { format: "jpeg", quality: 60 });
        await offscreenMessage({ op: "frame", data: shot.data });
      });
      frames++;
      const nextAt = started + frames * interval;
      const wait = nextAt - Date.now();
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    }
    const result = await offscreenMessage({ op: "finish" });
    if (!result || result.ok === false) throw new Error(result?.error || "GIF encoding failed");
    return {
      image: { data: result.data, mimeType: "image/gif" },
      data: { frames: result.frames, width: result.width, height: result.height },
      text: `recorded ${result.frames} frames (${result.width}x${result.height}) over ${duration}s as GIF`,
    };
  } finally {
    try {
      await offscreenMessage({ op: "reset" });
    } catch {}
  }
}
