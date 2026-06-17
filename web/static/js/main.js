/**
 * north Dashboard — main.js
 */

(function () {
  "use strict";

  // ── Auth ────────────────────────────────────────────────────────────────────
  const secretMeta = document.querySelector('meta[name="north-secret"]');
  const NORTH_SECRET = secretMeta ? secretMeta.getAttribute("content") : "";

  document.addEventListener("htmx:configRequest", function (evt) {
    if (NORTH_SECRET) evt.detail.headers["X-North-Secret"] = NORTH_SECRET;
  });

  function authHeaders() {
    return NORTH_SECRET ? { "X-North-Secret": NORTH_SECRET } : {};
  }

  // ── Helpers ──────────────────────────────────────────────────────────────────
  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function hasFiles(e) {
    return e.dataTransfer && Array.from(e.dataTransfer.types).includes("Files");
  }

  // ── Chat thread ──────────────────────────────────────────────────────────────
  const chatThread = document.getElementById("chat-thread");
  let chatHistory = [];

  // [icon, label] per SSE event. icon is a text glyph.
  const STEP_DEFS = {
    classifying:           ["◎", "Classifying your message…"],
    classified:            ["✓", "Classified"],
    classified_as_trivial: ["✓", "Quick task — skipping north star check"],
    north_star_checking:   ["★", "Checking north stars…"],
    north_star_aligned:    ["✓", "Aligned with your goals"],
    north_star_conflict:   ["!", "North star conflict — check approvals"],
    routing:               ["⇢", "Planning which agents to run…"],
    routed:                ["✓", "Execution plan ready"],
    executing:             ["▶", "Running agents…"],
    tool_called:           ["⚙", null],
    tool_result:           ["✓", null],
  };

  // Strip injected conversation history prefix so raw user message shows in bubbles.
  function stripHistoryPrefix(text) {
    const marker = "[Current message]\n";
    const idx = text.indexOf(marker);
    return idx !== -1 ? text.slice(idx + marker.length) : text;
  }

  function renderMarkdown(text) {
    if (typeof marked !== "undefined") {
      try { return marked.parse(text); } catch (_) {}
    }
    return escapeHtml(text).replace(/\n/g, "<br>");
  }

  async function loadChatHistory() {
    if (!chatThread) return;
    try {
      const resp = await fetch("/orchestrator/ledger?limit=200", { headers: authHeaders() });
      if (!resp.ok) return;
      const entries = await resp.json();

      const ordered = [...entries].reverse();
      const tasks = {};
      const taskOrder = [];

      for (const e of ordered) {
        if (!e.task_id) continue;
        if (e.source === "prompt" && e.action === "task_received") {
          tasks[e.task_id] = { prompt: stripHistoryPrefix(e.input || ""), time: e.timestamp, outputs: [] };
          taskOrder.push(e.task_id);
        }
        if (e.source === "agent" && e.action === "agent_completed" && tasks[e.task_id] && e.output) {
          tasks[e.task_id].outputs.push({ agent: e.agent, text: e.output });
        }
      }

      if (taskOrder.length === 0) return;

      const empty = chatThread.querySelector(".chat-empty");
      if (empty) empty.remove();

      for (const taskId of taskOrder) {
        const t = tasks[taskId];
        _appendUserBubble(t.prompt, taskId);
        if (t.outputs.length > 0) {
          _appendNorthBubble(t.outputs, taskId);
        }
      }

      const history = [];
      for (const taskId of taskOrder) {
        const t = tasks[taskId];
        const text = t.outputs
          .map(function (o) { return o.text; })
          .filter(Boolean)
          .join("\n\n");
        if (t.prompt && text) {
          history.push([t.prompt, text]);
        }
      }
      chatHistory = history.slice(-20);

      chatThread.scrollTop = chatThread.scrollHeight;
    } catch (_) {}
  }

  function _appendUserBubble(prompt, taskId) {
    if (!chatThread) return;
    const div = document.createElement("div");
    div.className = "chat-bubble user";
    div.id = "chat-user-" + taskId;
    div.innerHTML =
      '<span class="bubble-label">you</span>' +
      '<div class="bubble-content">' + escapeHtml(prompt) + '</div>';
    chatThread.appendChild(div);
  }

  function _appendThinkingBubble(taskId) {
    if (!chatThread) return;
    const div = document.createElement("div");
    div.className = "chat-bubble north";
    div.id = "chat-thinking-" + taskId;
    div.innerHTML =
      '<span class="bubble-label">north</span>' +
      '<div class="chat-thinking">' +
        '<span class="think-dot"></span>' +
        '<span class="think-dot"></span>' +
        '<span class="think-dot"></span>' +
      '</div>' +
      '<div class="bubble-steps" id="steps-' + taskId + '"></div>';
    chatThread.appendChild(div);
    chatThread.scrollTop = chatThread.scrollHeight;
  }

  function _addStep(taskId, icon, label) {
    const steps = document.getElementById("steps-" + taskId);
    if (!steps) return;
    // Mark previous active step as done
    const prev = steps.querySelector(".step-pill.step-active");
    if (prev) prev.classList.replace("step-active", "step-done");
    const pill = document.createElement("div");
    pill.className = "step-pill step-active";
    pill.innerHTML =
      '<span class="step-icon">' + escapeHtml(icon) + '</span>' +
      '<span>' + escapeHtml(label) + '</span>';
    steps.appendChild(pill);
    if (chatThread) chatThread.scrollTop = chatThread.scrollHeight;
  }

  function _appendNorthBubble(outputs, taskId) {
    if (!chatThread) return;
    const thinking = document.getElementById("chat-thinking-" + taskId);

    // Mark last active step as done and remove the thinking dots — keep steps
    if (thinking) {
      const lastActive = thinking.querySelector(".step-pill.step-active");
      if (lastActive) lastActive.classList.replace("step-active", "step-done");
      const dots = thinking.querySelector(".chat-thinking");
      if (dots) dots.remove();
    }

    const text = outputs
      .map(function (o) { return o.text; })
      .filter(Boolean)
      .join("\n\n") || "Task completed.";

    const bubble = thinking || document.getElementById("chat-north-" + taskId);
    if (bubble) {
      bubble.id = "chat-north-" + taskId;
      // Remove any pending approval widget — task is done
      const approvalWidget = bubble.querySelector(".approval-widget");
      if (approvalWidget) approvalWidget.remove();
      const content = document.createElement("div");
      content.className = "bubble-content markdown";
      content.innerHTML = renderMarkdown(text);
      bubble.appendChild(content);
    } else {
      const div = document.createElement("div");
      div.className = "chat-bubble north";
      div.id = "chat-north-" + taskId;
      div.innerHTML =
        '<span class="bubble-label">north</span>' +
        '<div class="bubble-content markdown">' + renderMarkdown(text) + '</div>';
      chatThread.appendChild(div);
    }
    chatThread.scrollTop = chatThread.scrollHeight;
  }

  function _appendErrorBubble(taskId, msg) {
    if (!chatThread) return;
    const thinking = document.getElementById("chat-thinking-" + taskId);
    if (thinking) {
      const lastActive = thinking.querySelector(".step-pill.step-active");
      if (lastActive) lastActive.classList.replace("step-active", "step-done");
      const dots = thinking.querySelector(".chat-thinking");
      if (dots) dots.remove();
      const content = document.createElement("div");
      content.className = "bubble-content chat-error";
      content.textContent = msg || "Task failed.";
      thinking.id = "chat-north-" + taskId;
      thinking.appendChild(content);
    } else {
      const div = document.createElement("div");
      div.className = "chat-bubble north";
      div.id = "chat-north-" + taskId;
      div.innerHTML =
        '<span class="bubble-label">north</span>' +
        '<div class="bubble-content chat-error">' + escapeHtml(msg || "Task failed.") + '</div>';
      chatThread.appendChild(div);
    }
    chatThread.scrollTop = chatThread.scrollHeight;
  }

  function subscribeToTask(taskId, cleanPrompt) {
    _appendThinkingBubble(taskId);

    // EventSource sends same-origin cookies automatically — auth handled via cookie.
    const es = new EventSource("/orchestrator/stream/" + taskId);

    // The server emits named SSE events (event: classified, event: routing, …).
    // onmessage only fires for unnamed events, so we must listen per event name.
    const NAMED_EVENTS = [
      "classifying", "classified", "classified_as_trivial",
      "north_star_checking", "north_star_aligned", "north_star_conflict",
      "routing", "routed", "executing",
      "agent_started", "agent_completed",
      "tool_called", "tool_result",
      "token",
      "reasoning",
      "task_synthesis",
      "approval_required",
      "task_completed", "task_failed", "task_cancelled",
    ];

    // Accumulate streamed tokens so task_completed can use them directly.
    let tokenBuffer = "";
    // The model's private chain-of-thought, shown dimmed and never part of the answer.
    let reasoningBuffer = "";

    function handleSseEvent(evt) {
      try {
        const data = JSON.parse(evt.data);
        const event = evt.type;

        if (event === "agent_started") {
          _addStep(taskId, "◎", (data.agent || "agent") + " agent running…");
        } else if (event === "agent_completed") {
          const label = data.summary
            ? (data.agent || "agent") + ": " + data.summary
            : (data.agent || "agent") + " agent done";
          _addStep(taskId, "✓", label);
        } else if (event === "tool_called") {
          _addStep(taskId, "⚙", "  " + (data.tool || "tool") + "…");
        } else if (event === "tool_result") {
          const icon = data.success === false ? "✗" : "✓";
          _addStep(taskId, icon, "  " + (data.tool || "tool") + " done");
        } else if (event === "token") {
          // Progressive token rendering — append to a live streaming div inside the
          // thinking bubble.  The accumulated text becomes the final answer when
          // task_completed fires, avoiding the extra ledger fetch.
          const token = data.text || "";
          tokenBuffer += token;
          const bubble = document.getElementById("chat-thinking-" + taskId);
          if (bubble) {
            // The answer has started — drop the dim reasoning preview.
            const stale = bubble.querySelector(".bubble-reasoning");
            if (stale) stale.remove();
            let streamDiv = bubble.querySelector(".bubble-streaming");
            if (!streamDiv) {
              streamDiv = document.createElement("div");
              streamDiv.className = "bubble-content bubble-streaming";
              bubble.appendChild(streamDiv);
            }
            streamDiv.innerHTML = renderMarkdown(tokenBuffer);
            chatThread.scrollTop = chatThread.scrollHeight;
          }
        } else if (event === "reasoning") {
          // Private reasoning — render dimmed, only until the answer streams.
          reasoningBuffer += data.text || "";
          const bubble = document.getElementById("chat-thinking-" + taskId);
          if (bubble && !bubble.querySelector(".bubble-streaming")) {
            let reasonDiv = bubble.querySelector(".bubble-reasoning");
            if (!reasonDiv) {
              reasonDiv = document.createElement("div");
              reasonDiv.className = "bubble-content bubble-reasoning";
              bubble.appendChild(reasonDiv);
            }
            reasonDiv.textContent = reasoningBuffer;
            chatThread.scrollTop = chatThread.scrollHeight;
          }
        } else if (event === "approval_required") {
          _addStep(taskId, "?", "Approval required");
          const bubble = document.getElementById("chat-thinking-" + taskId);
          if (bubble) {
            const widget = document.createElement("div");
            widget.className = "approval-widget";
            widget.innerHTML =
              '<div class="approval-message">' + escapeHtml(data.message || "Action requires your approval.") + '</div>' +
              '<div class="approval-options">' +
              (data.options || ["Approve", "Reject"]).map(function (opt) {
                return '<button class="btn btn-ghost approval-btn" data-option="' + escapeHtml(opt) + '">' + escapeHtml(opt) + '</button>';
              }).join("") +
              '</div>';
            widget.querySelectorAll(".approval-btn").forEach(function (btn) {
              btn.addEventListener("click", async function () {
                const chosen = this.dataset.option;
                const lower = chosen.toLowerCase();
                const decision = (lower === "approve" || lower === "approved") ? "approved"
                  : (lower === "reject" || lower === "rejected") ? "rejected"
                  : "answered";
                widget.innerHTML = '<span class="approval-sent">Decision sent: ' + escapeHtml(chosen) + '</span>';
                await fetch("/orchestrator/approval/respond", {
                  method: "POST",
                  headers: Object.assign({}, authHeaders(), { "Content-Type": "application/json" }),
                  body: JSON.stringify({
                    card_id: data.card_id,
                    task_id: taskId,
                    agent: data.agent || "",
                    decision: decision,
                    chosen_option: chosen,
                  }),
                });
              });
            });
            bubble.appendChild(widget);
            chatThread.scrollTop = chatThread.scrollHeight;
          }
        } else if (event === "task_synthesis") {
          // Multi-agent synthesis arrived — override token buffer with merged output.
          if (data.output) {
            tokenBuffer = data.output;
          }
          _addStep(taskId, "⟳", "Synthesising agent outputs…");
        } else if (STEP_DEFS[event] && STEP_DEFS[event][1]) {
          _addStep(taskId, STEP_DEFS[event][0], STEP_DEFS[event][1]);
        }

        if (event === "task_completed") {
          es.close();
          loadStrategyBadge();
          // Remove the live streaming div — the final bubble will replace it.
          const bubble = document.getElementById("chat-thinking-" + taskId);
          if (bubble) {
            const streamDiv = bubble.querySelector(".bubble-streaming");
            if (streamDiv) streamDiv.remove();
          }
          if (tokenBuffer) {
            // Tokens were streamed — use them directly without a ledger round-trip.
            _appendNorthBubble([{ agent: "", text: tokenBuffer }], taskId);
            if (cleanPrompt) {
              chatHistory.push([cleanPrompt, tokenBuffer]);
              if (chatHistory.length > 20) chatHistory = chatHistory.slice(-20);
            }
            tokenBuffer = "";
          } else {
            // No tokens (e.g. multi-agent synthesis path) — fetch from ledger.
            setTimeout(async function () {
              try {
                const resp = await fetch(
                  "/orchestrator/ledger?task_id=" + taskId + "&limit=20",
                  { headers: authHeaders() }
                );
                if (!resp.ok) { _appendNorthBubble([], taskId); return; }
                const entries = await resp.json();
                const outputs = entries
                  .filter(function (e) { return e.action === "agent_completed" && e.output; })
                  .map(function (e) { return { agent: e.agent, text: e.output }; });
                _appendNorthBubble(outputs.length ? outputs : [], taskId);
                if (cleanPrompt) {
                  const text = outputs.map(function (o) { return o.text; }).filter(Boolean).join("\n\n") || "Task completed.";
                  chatHistory.push([cleanPrompt, text]);
                  if (chatHistory.length > 20) chatHistory = chatHistory.slice(-20);
                }
              } catch (_) {
                _appendNorthBubble([], taskId);
              }
            }, 500);
          }
        }

        if (event === "task_failed") {
          es.close();
          _appendErrorBubble(taskId, data.error || "Task failed.");
        }

        if (event === "task_cancelled") {
          es.close();
          tokenBuffer = "";
          const reason = data.reason || "Task cancelled.";
          _appendErrorBubble(taskId, reason);
        }
      } catch (_) {}
    }

    NAMED_EVENTS.forEach(function (name) {
      es.addEventListener(name, handleSseEvent);
    });

    es.onerror = function () { es.close(); };
  }

  // ── File chip ────────────────────────────────────────────────────────────────
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
    if (taskInput)  taskInput.placeholder = "Describe what to do with " + file.name + " (optional)";
  }

  function clearFileChip() {
    pendingFile = null;
    if (fileChip)   fileChip.style.display = "none";
    if (fileNameEl) fileNameEl.textContent = "";
    if (taskInput)  taskInput.placeholder = "What would you like north to do?";
  }

  if (fileRemove) fileRemove.addEventListener("click", clearFileChip);

  // ── Task form submit ─────────────────────────────────────────────────────────
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

        let promptToPost = finalPrompt;
        if (chatHistory.length > 0) {
          const turns = chatHistory.slice(-5).map(function (turn) {
            return "User: " + turn[0] + "\nAssistant: " + turn[1];
          }).join("\n");
          promptToPost = "[Conversation so far]\n" + turns + "\n\n[Current message]\n" + finalPrompt;
        }

        const taskResp = await fetch("/orchestrator/task", {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/x-www-form-urlencoded" },
          body: "prompt=" + encodeURIComponent(promptToPost),
        });

        if (taskResp.ok) {
          const taskData = await taskResp.json();
          taskInput.value = "";

          // Remove empty state and append user bubble
          const empty = chatThread && chatThread.querySelector(".chat-empty");
          if (empty) empty.remove();
          _appendUserBubble(finalPrompt, taskData.task_id);
          subscribeToTask(taskData.task_id, finalPrompt);

          // Also inject into the activity feed
          const feedEl = document.getElementById("feed");
          if (feedEl) {
            const item = document.createElement("div");
            item.className = "feed-item feed-pending";
            item.innerHTML =
              '<div class="feed-meta">' +
                '<span class="feed-source">prompt</span>' +
                '<span class="feed-time">' +
                  new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) +
                '</span>' +
              '</div>' +
              '<div class="feed-action">' + escapeHtml(finalPrompt) + '</div>' +
              '<div class="feed-task-id">' + escapeHtml(taskData.task_id || "") + '</div>';
            const feedEmpty = feedEl.querySelector(".feed-empty");
            if (feedEmpty) feedEmpty.remove();
            feedEl.prepend(item);
          }
        } else {
          taskInput.placeholder = "Error submitting — try again";
          setTimeout(function () { taskInput.placeholder = "What would you like north to do?"; }, 3000);
        }
      } catch (_) {
        taskInput.placeholder = "Network error — try again";
        setTimeout(function () { taskInput.placeholder = "What would you like north to do?"; }, 3000);
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

  // ── Command bar drag-and-drop ────────────────────────────────────────────────
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
      e.preventDefault(); depth = 0;
      commandBar.classList.remove("drag-active");
      const file = e.dataTransfer.files[0];
      if (file) showFileChip(file);
    });
  }

  // ── Context page drop zone ────────────────────────────────────────────────────
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
      e.preventDefault(); depth = 0;
      ctxZone.classList.remove("drag-active");
      const file = e.dataTransfer.files[0];
      if (file) await uploadContextFile(file);
    });

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
          const err = await resp.json().catch(function () { return {}; });
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

  // ── SSE activity feed highlights ─────────────────────────────────────────────
  document.addEventListener("htmx:sseMessage", function (evt) {
    try {
      const data = JSON.parse(evt.detail.data);
      const taskId = data.task_id;
      if (!taskId) return;
      const card = document.getElementById("task-" + taskId);
      if (!card) return;
      card.classList.add("sse-flash");
      setTimeout(function () { card.classList.remove("sse-flash"); }, 600);
    } catch (_) {}
  });

  // ── Animate new feed items ────────────────────────────────────────────────────
  const feedEl = document.getElementById("feed");
  if (feedEl) {
    const obs = new MutationObserver(function (mutations) {
      mutations.forEach(function (mut) {
        mut.addedNodes.forEach(function (node) {
          if (node.classList && node.classList.contains("feed-item")) {
            node.style.animation = "none";
            requestAnimationFrame(function () { node.style.animation = ""; });
          }
        });
      });
    });
    obs.observe(feedEl, { childList: true });
  }

  // ── Local time conversion ──────────────────────────────────────────────────────
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
        const p = function (n) { return String(n).padStart(2, "0"); };
        el.textContent =
          date.getFullYear() + "-" + p(date.getMonth() + 1) + "-" + p(date.getDate()) +
          " " + p(date.getHours()) + ":" + p(date.getMinutes());
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", convertLocalTimes);
  } else {
    convertLocalTimes();
  }
  document.addEventListener("htmx:afterSwap", convertLocalTimes);

  // ── Strategy badge ────────────────────────────────────────────────────────────
  async function loadStrategyBadge() {
    try {
      const resp = await fetch("/orchestrator/settings", { headers: authHeaders() });
      if (!resp.ok) return;
      const data = await resp.json();
      const bar = document.querySelector(".command-bar");
      if (!bar) return;
      let badge = document.getElementById("strategy-badge");
      if (!badge) {
        badge = document.createElement("span");
        badge.id = "strategy-badge";
        bar.insertBefore(badge, bar.firstChild);
      }
      badge.textContent = data.strategy;
      badge.className = "strategy-badge strategy-" + data.strategy;
    } catch (_) {}
  }

  // ── Boot ───────────────────────────────────────────────────────────────────────
  loadChatHistory();
  loadStrategyBadge();

})();
