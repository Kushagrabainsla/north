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
    taskForm.addEventListener("submit", function (evt) {
      evt.preventDefault();

      const prompt = taskInput.value.trim();
      if (!prompt) return;

      // Show submitting state
      taskInput.disabled = true;
      taskInput.value = "";
      taskInput.placeholder = "Submitting task...";

      // Send the request via fetch
      const headers = {
        "Content-Type": "application/json",
      };
      if (NORTH_SECRET) {
        headers["X-North-Secret"] = NORTH_SECRET;
      }

      fetch("/orchestrator/task", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({ prompt: prompt }),
      })
      .then(response => {
        if (!response.ok) {
          throw new Error("Failed to submit task");
        }
        return response.json();
      })
      .then(data => {
        taskInput.placeholder = "Task submitted! ✓";
        setTimeout(() => {
          window.location.reload();
        }, 1000);
      })
      .catch(error => {
        console.error("Error submitting task:", error);
        taskInput.disabled = false;
        taskInput.value = prompt; // Restore original input
        taskInput.placeholder = "Error submitting task. Try again.";
        setTimeout(() => {
          taskInput.placeholder = "What would you like north to do?";
        }, 3000);
      });
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

  // ── Timezone Conversion ──────────────────────────────────────────────────
  function convertLocalTimes() {
    document.querySelectorAll(".local-time").forEach((el) => {
      const utcStr = el.getAttribute("data-utc");
      const format = el.getAttribute("data-format");
      if (!utcStr) return;

      const date = new Date(utcStr);
      if (isNaN(date.getTime())) return;

      if (format === "time") {
        el.textContent = date.toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        });
      } else {
        // Format as YYYY-MM-DD HH:MM
        const yyyy = date.getFullYear();
        const mm = String(date.getMonth() + 1).padStart(2, "0");
        const dd = String(date.getDate()).padStart(2, "0");
        const hh = String(date.getHours()).padStart(2, "0");
        const min = String(date.getMinutes()).padStart(2, "0");

        if (el.classList.contains("ledger-time") && el.textContent.split(":").length === 3) {
          const ss = String(date.getSeconds()).padStart(2, "0");
          el.textContent = `${yyyy}-${mm}-${dd} ${hh}:${min}:${ss}`;
        } else {
          el.textContent = `${yyyy}-${mm}-${dd} ${hh}:${min}`;
        }
      }
    });
  }

  // Run on load
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", convertLocalTimes);
  } else {
    convertLocalTimes();
  }
  // Also run when HTMX swaps content (so newly loaded elements get converted too!)
  document.addEventListener("htmx:afterSwap", convertLocalTimes);
})();
