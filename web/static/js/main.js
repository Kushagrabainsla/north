/**
 * north Dashboard — main.js
 */

(function () {
  "use strict";

  // ── Secret header injection ─────────────────────────────────────────────
  const secretMeta = document.querySelector('meta[name="north-secret"]');
  const NORTH_SECRET = secretMeta ? secretMeta.getAttribute("content") : "";

  document.addEventListener("htmx:configRequest", function (evt) {
    if (NORTH_SECRET) {
      evt.detail.headers["X-North-Secret"] = NORTH_SECRET;
    }
  });

  function authHeaders() {
    return NORTH_SECRET ? { "X-North-Secret": NORTH_SECRET } : {};
  }

  // ── Task form UX ────────────────────────────────────────────────────────
  const taskForm   = document.getElementById("task-form");
  const taskInput  = document.getElementById("task-prompt");
  const fileChip   = document.getElementById("task-file-chip");
  const fileNameEl = document.getElementById("task-file-name");
  const fileRemove = document.getElementById("task-file-remove");

  let pendingFile = null;

  function showFileChip(file) {
    pendingFile = file;
    if (fileNameEl) fileNameEl.textContent = file.name;
    if (fileChip)   fileChip.style.display = "inline-flex";
    taskInput.placeholder = "Describe what to do with " + file.name + " (optional)";
  }

  function clearFileChip() {
    pendingFile = null;
    if (fileChip)   fileChip.style.display = "none";
    if (fileNameEl) fileNameEl.textContent = "";
    taskInput.placeholder = "What would you like north to do?";
  }

  if (fileRemove) fileRemove.addEventListener("click", clearFileChip);

  if (taskForm && taskInput) {
    taskForm.addEventListener("submit", async function (evt) {
      evt.preventDefault();
      const prompt = taskInput.value.trim();
      if (!prompt && !pendingFile) return;

      const submitBtn = taskForm.querySelector("button[type=submit]");
      taskInput.disabled = true;
      if (submitBtn) submitBtn.disabled = true;

      try {
        let finalPrompt = prompt;

        // Step 1: upload attached file to context if present
        if (pendingFile) {
          const fd = new FormData();
          fd.append("file", pendingFile, pendingFile.name);
          const upResp = await fetch("/orchestrator/context/add", {
            method: "POST",
            headers: authHeaders(),
            body: fd,
          });
          if (upResp.ok) {
            const upData = await upResp.json();
            const docRef = upData.document || "context";
            finalPrompt = finalPrompt
              ? finalPrompt + " (uploaded: " + pendingFile.name + " → " + docRef + ")"
              : "I've uploaded " + pendingFile.name + " to context. Please review it.";
          } else {
            finalPrompt = finalPrompt || "Process the file: " + pendingFile.name;
          }
          clearFileChip();
        }

        // Step 2: submit the task
        const taskResp = await fetch("/orchestrator/task", {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/x-www-form-urlencoded" },
          body: "prompt=" + encodeURIComponent(finalPrompt),
        });

        if (taskResp.ok) {
          const taskData = await taskResp.json();
          taskInput.value = "";
          taskInput.placeholder = "Task submitted ✓";
          setTimeout(() => {
            taskInput.placeholder = "What would you like north to do?";
          }, 2000);

          // Inject a live feed item without page reload
          const feedEl = document.getElementById("feed");
          if (feedEl) {
            const item = document.createElement("div");
            item.className = "feed-item feed-pending";
            item.innerHTML =
              '<div class="feed-meta">' +
                '<span class="feed-source">prompt</span>' +
                '<span class="feed-time">' + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) + '</span>' +
              '</div>' +
              '<div class="feed-action">' + escapeHtml(finalPrompt) + '</div>' +
              '<div class="feed-task-id">' + escapeHtml(taskData.task_id || "") + '</div>';
            const empty = feedEl.querySelector(".feed-empty");
            if (empty) empty.remove();
            feedEl.prepend(item);
          }
        } else {
          taskInput.placeholder = "Error submitting — try again";
          setTimeout(() => { taskInput.placeholder = "What would you like north to do?"; }, 3000);
        }
      } catch (_) {
        taskInput.placeholder = "Network error — try again";
        setTimeout(() => { taskInput.placeholder = "What would you like north to do?"; }, 3000);
      } finally {
        taskInput.disabled = false;
        if (submitBtn) submitBtn.disabled = false;
        taskInput.focus();
      }
    });

    taskInput.addEventListener("keydown", function (evt) {
      if (evt.key === "Enter" && !evt.shiftKey) {
        evt.preventDefault();
        taskForm.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      }
    });
  }

  // ── Command bar drag-and-drop ────────────────────────────────────────────
  const commandBar = document.querySelector(".command-bar");
  if (commandBar) {
    let depth = 0;

    commandBar.addEventListener("dragenter", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (++depth === 1) commandBar.classList.add("drag-active");
    });
    commandBar.addEventListener("dragover",  function (e) { if (hasFiles(e)) e.preventDefault(); });
    commandBar.addEventListener("dragleave", function ()  { if (--depth <= 0) { depth = 0; commandBar.classList.remove("drag-active"); } });
    commandBar.addEventListener("drop", function (e) {
      e.preventDefault();
      depth = 0;
      commandBar.classList.remove("drag-active");
      const file = e.dataTransfer.files[0];
      if (file) showFileChip(file);
    });
  }

  // ── Context page drop zone ───────────────────────────────────────────────
  const ctxZone   = document.getElementById("context-drop-zone");
  const ctxStatus = document.getElementById("context-drop-status");

  if (ctxZone) {
    let depth = 0;

    ctxZone.addEventListener("dragenter", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (++depth === 1) ctxZone.classList.add("drag-active");
    });
    ctxZone.addEventListener("dragover",  function (e) { if (hasFiles(e)) e.preventDefault(); });
    ctxZone.addEventListener("dragleave", function ()  { if (--depth <= 0) { depth = 0; ctxZone.classList.remove("drag-active"); } });
    ctxZone.addEventListener("drop", async function (e) {
      e.preventDefault();
      depth = 0;
      ctxZone.classList.remove("drag-active");
      const file = e.dataTransfer.files[0];
      if (file) await uploadContextFile(file);
    });

    // Click to browse
    ctxZone.addEventListener("click", function () {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = ".pdf,.docx,.doc,.txt,.md,.csv";
      input.onchange = async function () {
        if (input.files[0]) await uploadContextFile(input.files[0]);
      };
      input.click();
    });

    async function uploadContextFile(file) {
      setCtxStatus("Uploading " + file.name + "…", "");
      const fd = new FormData();
      fd.append("file", file, file.name);
      try {
        const resp = await fetch("/orchestrator/context/add", {
          method: "POST",
          headers: authHeaders(),
          body: fd,
        });
        if (resp.ok) {
          const data = await resp.json();
          setCtxStatus("✓ Injected into " + (data.document || "context"), "ok");
        } else {
          const err = await resp.json().catch(() => ({}));
          setCtxStatus("✗ " + (err.detail || "Upload failed"), "err");
        }
      } catch (_) {
        setCtxStatus("✗ Network error", "err");
      }
    }

    function setCtxStatus(msg, cls) {
      if (!ctxStatus) return;
      ctxStatus.textContent = msg;
      ctxStatus.className = "drop-zone__status" + (cls ? " " + cls : "");
    }
  }

  // ── SSE event highlights ─────────────────────────────────────────────────
  document.addEventListener("htmx:sseMessage", function (evt) {
    try {
      const data = JSON.parse(evt.detail.data);
      const taskId = data.task_id;
      if (!taskId) return;
      const card = document.getElementById("task-" + taskId);
      if (!card) return;
      card.classList.add("sse-flash");
      setTimeout(() => card.classList.remove("sse-flash"), 600);
      if (data.event === "task_completed") {
        const badge = card.querySelector(".task-status");
        if (badge) { badge.textContent = "completed"; badge.className = "task-status status-completed"; }
        const bar = card.querySelector(".progress-bar");
        if (bar) bar.style.width = "100%";
      }
      if (data.event === "agent_failed") {
        const badge = card.querySelector(".task-status");
        if (badge) { badge.textContent = "failed"; badge.className = "task-status status-failed"; }
      }
    } catch (_) {}
  });

  // ── Animate newly inserted feed items ───────────────────────────────────
  const observer = new MutationObserver(function (mutations) {
    mutations.forEach(function (mut) {
      mut.addedNodes.forEach(function (node) {
        if (node.classList && node.classList.contains("feed-item")) {
          node.style.animation = "none";
          requestAnimationFrame(() => { node.style.animation = ""; });
        }
      });
    });
  });
  const feed = document.getElementById("feed");
  if (feed) observer.observe(feed, { childList: true });

  // ── Local time conversion ────────────────────────────────────────────────
  function convertLocalTimes() {
    document.querySelectorAll(".local-time").forEach(function (el) {
      const utcStr = el.getAttribute("data-utc");
      const format = el.getAttribute("data-format");
      if (!utcStr) return;
      const date = new Date(utcStr);
      if (isNaN(date.getTime())) return;
      if (format === "time") {
        el.textContent = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
      } else {
        const p = (n) => String(n).padStart(2, "0");
        el.textContent = date.getFullYear() + "-" + p(date.getMonth() + 1) + "-" + p(date.getDate()) + " " + p(date.getHours()) + ":" + p(date.getMinutes());
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", convertLocalTimes);
  } else {
    convertLocalTimes();
  }
  document.addEventListener("htmx:afterSwap", convertLocalTimes);

  // ── Helpers ──────────────────────────────────────────────────────────────
  function hasFiles(e) {
    return e.dataTransfer && Array.from(e.dataTransfer.types).includes("Files");
  }

  function escapeHtml(str) {
    return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
})();
