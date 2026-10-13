/**
 * Page-level ops behind read_page / find / get_page_text / form_input /
 * file_upload / upload_image. Injected on demand, top frame only.
 *
 * Walks the DOM and builds an accessibility-style snapshot in which every
 * visible, possibly-interactive element gets a stable-for-this-snapshot ref
 * (e1, e2, ...). The ref -> element map lives in the page until the next walk.
 */
(() => {
  if (globalThis.__agentTabContent) return;
  globalThis.__agentTabContent = true;

  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "META", "LINK", "TITLE", "HEAD", "BASE", "SVG"]);
  const MAX_TEXT = 80;

  let refMap = new Map();
  let refCounter = 0;

  function isVisible(el, style) {
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return false;
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName;
    switch (tag) {
      case "A": return "link";
      case "BUTTON": return "button";
      case "SELECT": return "combobox";
      case "TEXTAREA": return "textbox";
      case "INPUT": {
        const type = (el.type || "text").toLowerCase();
        if (type === "checkbox") return "checkbox";
        if (type === "radio") return "radio";
        if (type === "button" || type === "submit" || type === "reset") return "button";
        if (type === "file") return "file-input";
        if (type === "hidden") return null;
        return "textbox";
      }
      case "IMG": return "img";
      case "H1": case "H2": case "H3": case "H4": case "H5": case "H6": return `heading${tag.slice(1)}`;
      case "NAV": return "navigation";
      case "MAIN": return "main";
      case "ASIDE": return "complementary";
      case "FOOTER": return "contentinfo";
      case "HEADER": return "banner";
      case "FORM": return "form";
      case "TABLE": return "table";
      case "UL": case "OL": return "list";
      case "LI": return "listitem";
      case "IFRAME": return "iframe";
      case "VIDEO": return "video";
      case "AUDIO": return "audio";
      case "CANVAS": return "canvas";
      default: return null;
    }
  }

  function textOf(el) {
    return (el.textContent || "").replace(/\s+/g, " ").trim();
  }

  function nameOf(el, style) {
    const label = el.getAttribute("aria-label");
    if (label) return label.trim().slice(0, MAX_TEXT);

    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const t = labelledby
        .split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map(textOf)
        .join(" ")
        .trim();
      if (t) return t.slice(0, MAX_TEXT);
    }

    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      if (el.id) {
        const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (label) return textOf(label).slice(0, MAX_TEXT) || el.getAttribute("placeholder") || el.name || el.id;
      }
      const closest = el.closest("label");
      if (closest) return (textOf(closest) || el.getAttribute("placeholder") || el.name || "").slice(0, MAX_TEXT);
      return (el.getAttribute("placeholder") || el.name || el.getAttribute("title") || "").slice(0, MAX_TEXT);
    }

    if (el.tagName === "IMG") return (el.getAttribute("alt") || el.getAttribute("title") || "").slice(0, MAX_TEXT);

    const ownText = Array.from(el.childNodes)
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent.replace(/\s+/g, " ").trim())
      .join(" ")
      .trim();
    if (ownText) return ownText.slice(0, MAX_TEXT);

    if (el.tagName === "A" || el.tagName === "BUTTON") {
      const t = textOf(el);
      if (t) return t.slice(0, MAX_TEXT);
    }

    return (el.getAttribute("title") || "").slice(0, MAX_TEXT);
  }

  function valueOf(el) {
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      if (el.type === "checkbox" || el.type === "radio") return el.checked ? "checked" : "";
      return (el.value || "").slice(0, MAX_TEXT);
    }
    if (el.tagName === "SELECT") {
      const opt = el.selectedOptions?.[0];
      return opt ? opt.text.trim().slice(0, MAX_TEXT) : "";
    }
    if (el.isContentEditable) return textOf(el).slice(0, MAX_TEXT);
    return null;
  }

  function describe(el) {
    const role = roleOf(el);
    const name = nameOf(el);
    const value = valueOf(el);
    const tag = el.tagName.toLowerCase() + (el.type ? `[type=${el.type}]` : "");
    let line = role ? `${role}` : `${tag}`;
    if (name) line += ` "${name}"`;
    if (!role || !name) line += ` <${tag}>`;
    if (value) line += ` value="${value}"`;
    if (el.disabled) line += " [disabled]";
    if (el.required) line += " [required]";
    if (el.checked) line += " [checked]";
    if (el.tagName === "A" && el.getAttribute("href")) line += ` href=${el.getAttribute("href").slice(0, 60)}`;
    return line;
  }

  function walk(el, depth, out, includeAll) {
    if (!el || SKIP_TAGS.has(el.tagName)) return;
    if (el.type === "hidden") return;
    const style = getComputedStyle(el);
    if (!isVisible(el, style)) return;

    const role = roleOf(el);
    const interactive = el.tagName === "INPUT" || el.tagName === "BUTTON" || el.tagName === "SELECT" ||
      el.tagName === "TEXTAREA" || el.tagName === "A" || el.isContentEditable ||
      el.getAttribute("onclick") || el.getAttribute("role") === "button";
    const hasName = !!nameOf(el);
    const isLeaf = el.children.length === 0 || el.tagName === "IMG" || el.tagName === "IFRAME";

    if (includeAll || role || interactive || (isLeaf && hasName)) {
      const ref = `e${++refCounter}`;
      refMap.set(ref, el);
      out.push({ depth, ref, text: `${describe(el)} (${ref})` });
    }

    if (el.shadowRoot) {
      for (const child of el.shadowRoot.children) walk(child, depth + 1, out, false);
    }
    for (const child of el.children) walk(child, depth + 1, out, false);
  }

  function snapshot(includeAll) {
    refMap = new Map();
    refCounter = 0;
    const out = [];
    const root = document.body || document.documentElement;
    walk(root, 0, out, includeAll);
    return out
      .slice(0, 3000)
      .map((n) => `${"  ".repeat(n.depth)}- ${n.text}`)
      .join("\n");
  }

  function resolveTarget({ ref, selector }) {
    if (ref) {
      const el = refMap.get(ref);
      if (el) return el;
      throw new Error(`ref "${ref}" is stale — run read_page or find again`);
    }
    if (selector) {
      const el = document.querySelector(selector);
      if (el) return el;
      throw new Error(`no element matches selector "${selector}"`);
    }
    return null;
  }

  function nativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function formInput({ value, ref, selector, clear }) {
    const el = resolveTarget({ ref, selector });
    if (!el) throw new Error("form_input needs a ref (from read_page/find) or a selector");
    el.focus?.();
    let target = String(value);

    if (el.tagName === "INPUT" && (el.type === "checkbox" || el.type === "radio")) {
      const want = value === true || value === "true" || value === 1 || value === "1";
      if (el.type === "checkbox") el.checked = want;
      else if (want) el.checked = true;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { target: `${el.tagName.toLowerCase()}[type=${el.type}]`, value: el.checked };
    }

    if (el.tagName === "SELECT") {
      const want = String(value);
      const opt = Array.from(el.options).find((o) => o.value === want || o.text.trim() === want);
      if (!opt) {
        const options = Array.from(el.options).map((o) => o.value).slice(0, 30).join(", ");
        throw new Error(`option "${want}" not in select; options: ${options}`);
      }
      el.value = opt.value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { target: "select", value: opt.value };
    }

    if (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && !["checkbox", "radio", "file", "button", "submit", "reset", "image"].includes(el.type))) {
      nativeValue(el, target);
      return { target: el.tagName.toLowerCase(), value: el.value };
    }

    if (el.isContentEditable) {
      el.focus();
      if (clear !== false) document.getSelection().selectAllChildren(el);
      document.execCommand("insertText", false, target);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return { target: "contenteditable", value: textOf(el).slice(0, 100) };
    }

    // Last resort: set value if the element has one.
    if ("value" in el) {
      nativeValue(el, target);
      return { target: el.tagName.toLowerCase(), value: el.value };
    }
    throw new Error(`element <${el.tagName.toLowerCase()}> is not a form field`);
  }

  function bytesOf(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  function fileInputFor(ref, selector) {
    if (ref || selector) {
      const el = resolveTarget({ ref, selector });
      if (el.tagName === "INPUT" && el.type === "file") return el;
      throw new Error(`target ${ref || selector} is not a file input`);
    }
    const visible = Array.from(document.querySelectorAll('input[type="file"]')).find((input) => {
      const style = getComputedStyle(input);
      return isVisible(input, style) || input.closest("form");
    });
    if (!visible) throw new Error("no visible file input found on the page — pass a selector");
    return visible;
  }

  function fileUpload({ files, ref, selector }) {
    if (!files?.length) throw new Error("file_upload needs files: [{name, data(base64), mimeType?}]");
    const input = fileInputFor(ref, selector);
    const dt = new DataTransfer();
    for (const f of files) {
      dt.items.add(new File([bytesOf(f.data)], f.name || "upload.bin", { type: f.mimeType || "application/octet-stream" }));
    }
    input.files = dt.files;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return { target: `input[type=file]${input.name ? `[name=${input.name}]` : ""}`, names: Array.from(input.files).map((f) => f.name) };
  }

  function uploadImage({ data, mimeType = "image/png", name = "image.png", ref, selector }) {
    if (!data) throw new Error("upload_image needs data (base64 image)");
    const target = (ref || selector) ? resolveTarget({ ref, selector }) : document.activeElement || document.body;

    if (target.tagName === "INPUT" && target.type === "file") {
      return fileUpload({ files: [{ data, mimeType, name }], ref, selector });
    }

    const dt = new DataTransfer();
    dt.items.add(new File([bytesOf(data)], name, { type: mimeType }));
    const pasteInit = { bubbles: true, cancelable: true, clipboardData: dt };
    let delivered = false;
    try {
      delivered = target.dispatchEvent(new ClipboardEvent("paste", pasteInit));
    } catch {}
    try {
      target.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt }));
    } catch {}
    if (!delivered) {
      document.dispatchEvent(new ClipboardEvent("paste", pasteInit));
    }
    return { target: target.tagName.toLowerCase(), summary: `dispatched paste/drop with ${name} (${mimeType}) on <${target.tagName.toLowerCase()}>` };
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type !== "taskwindow:op") return;
    (async () => {
      try {
        switch (msg.op) {
          case "read_page": {
            const tree = snapshot(false);
            sendResponse({ ok: true, text: tree, refCount: refMap.size });
            break;
          }
          case "find": {
            const query = String(msg.params.query || "").toLowerCase();
            if (!query) throw new Error("find needs a query");
            // Rebuild refs for the whole page so returned refs are usable.
            refMap = new Map();
            refCounter = 0;
            const nodes = [];
            walk(document.body || document.documentElement, 0, nodes, false);
            const matches = nodes
              .filter((n) => n.text.toLowerCase().includes(query))
              .slice(0, 50)
              .map((n) => `${"  ".repeat(n.depth)}- ${n.text}`);
            sendResponse({ ok: true, matches, refCount: refMap.size });
            break;
          }
          case "get_page_text": {
            const max = msg.params.maxLength || 50000;
            const text = (document.body?.innerText || document.documentElement.innerText || "").slice(0, max);
            sendResponse({ ok: true, text: text + (text.length >= max ? "\n…[truncated]" : "") });
            break;
          }
          case "form_input": {
            const result = formInput(msg.params);
            sendResponse({ ok: true, result });
            break;
          }
          case "file_upload": {
            const result = fileUpload(msg.params);
            sendResponse({ ok: true, result });
            break;
          }
          case "upload_image": {
            const result = uploadImage(msg.params);
            sendResponse({ ok: true, result, summary: result.summary });
            break;
          }
          default:
            sendResponse({ ok: false, error: `unknown op "${msg.op}"` });
        }
      } catch (err) {
        sendResponse({ ok: false, error: err?.message || String(err) });
      }
    })();
    return true; // async response
  });
})();
