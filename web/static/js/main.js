/**
 * north Dashboard — main.js
 *
 * Handles task form submission, injects the secret header into HTMX requests,
 * and provides UX polish (auto-clear input, feed animations).
 */

(function () {
  "use strict";

  // ── Secret header injection ─────────────────────────────────────────────
  // The X-North-Secret is loaded from the meta tag injected server-side,
  // or falls back to an empty string (dev mode without auth enforcement).
  const secretMeta = document.querySelector('meta[name="north-secret"]');
  const NORTH_SECRET = secretMeta ? secretMeta.getAttribute("content") : "";

  // Inject the auth header into every HTMX request automatically.
  document.addEventListener("htmx:configRequest", function (evt) {
    if (NORTH_SECRET) {
      evt.detail.headers["X-North-Secret"] = NORTH_SECRET;
    }
  });

  // ── Task form UX ────────────────────────────────────────────────────────
  const taskForm = document.getElementById("task-form");
  const taskInput = document.getElementById("task-prompt");

  if (taskForm && taskInput) {
    // Clear input and show confirmation after successful submit
    taskForm.addEventListener("htmx:afterRequest", function (evt) {
      if (evt.detail.successful) {
        taskInput.value = "";
        taskInput.placeholder = "Task submitted! ✓";
        setTimeout(() => {
          taskInput.placeholder = "What would you like north to do?";
        }, 2000);
      }
    });

    // Submit on Enter
    taskInput.addEventListener("keydown", function (evt) {
      if (evt.key === "Enter" && !evt.shiftKey) {
        evt.preventDefault();
        taskForm.dispatchEvent(new Event("submit", { bubbles: true }));
      }
    });
  }

  // ── SSE event highlights ─────────────────────────────────────────────────
  // Flash task cards on incoming SSE events
  document.addEventListener("htmx:sseMessage", function (evt) {
    try {
      const data = JSON.parse(evt.detail.data);
      const taskId = data.task_id;
      if (!taskId) return;

      const card = document.getElementById("task-" + taskId);
      if (!card) return;

      card.classList.add("sse-flash");
      setTimeout(() => card.classList.remove("sse-flash"), 600);

      // Update status badge if event carries a status transition
      if (data.event === "task_completed") {
        const badge = card.querySelector(".task-status");
        if (badge) {
          badge.textContent = "completed";
          badge.className = "task-status status-completed";
        }
        const progress = card.querySelector(".progress-bar");
        if (progress) progress.style.width = "100%";
      }

      if (data.event === "agent_failed") {
        const badge = card.querySelector(".task-status");
        if (badge) {
          badge.textContent = "failed";
          badge.className = "task-status status-failed";
        }
      }
    } catch (_) {}
  });

  // ── Animate newly inserted feed items ───────────────────────────────────
  // HTMX inserts new nodes before existing ones; ensure animation runs.
  const observer = new MutationObserver(function (mutations) {
    mutations.forEach(function (mutation) {
      mutation.addedNodes.forEach(function (node) {
        if (node.classList && node.classList.contains("feed-item")) {
          node.style.animation = "none";
          requestAnimationFrame(() => {
            node.style.animation = "";
          });
        }
      });
    });
  });

  const feed = document.getElementById("feed");
  if (feed) {
    observer.observe(feed, { childList: true });
  }
})();
