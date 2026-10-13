/**
 * Offscreen document: decodes captured JPEG/PNG frames onto a canvas and
 * encodes them into an animated GIF89a (MV3 service workers have no Canvas).
 */
import { encodeGif } from "./gif-encoder.js";

let frames = [];
let maxWidth = 800;

function decodeFrame(dataUrl) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("frame failed to decode"));
    img.src = `data:image/jpeg;base64,${dataUrl}`;
  });
}

function imageDataFrom(img) {
  const scale = img.width > maxWidth ? maxWidth / img.width : 1;
  const w = Math.max(1, Math.round(img.width * scale));
  const h = Math.max(1, Math.round(img.height * scale));
  const canvas = new OffscreenCanvas(w, h);
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, w, h);
  return { rgba: ctx.getImageData(0, 0, w, h).data, w, h };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== "offscreen") return;
  (async () => {
    try {
      switch (msg.op) {
        case "begin":
          frames = [];
          maxWidth = Math.min(Math.max(msg.maxWidth || 800, 100), 1280);
          sendResponse({ ok: true });
          break;
        case "frame": {
          const img = await decodeFrame(msg.data);
          frames.push(imageDataFrom(img));
          if (frames.length > 200) frames.shift();
          sendResponse({ ok: true, frames: frames.length });
          break;
        }
        case "finish": {
          if (frames.length === 0) throw new Error("no frames captured");
          // Uniform delay; actual capture spacing varies slightly, that's fine.
          const gif = encodeGif(frames.map((f) => ({ rgba: f.rgba, width: f.w, height: f.h })), { delayMs: 200 });
          let binary = "";
          const chunk = 0x8000;
          for (let i = 0; i < gif.length; i += chunk) {
            binary += String.fromCharCode(...gif.subarray(i, i + chunk));
          }
          sendResponse({ ok: true, data: btoa(binary), frames: frames.length, width: frames[0].w, height: frames[0].h });
          break;
        }
        case "reset":
          frames = [];
          sendResponse({ ok: true });
          break;
        default:
          sendResponse({ ok: false, error: `unknown offscreen op "${msg.op}"` });
      }
    } catch (err) {
      sendResponse({ ok: false, error: err?.message || String(err) });
    }
  })();
  return true;
});
