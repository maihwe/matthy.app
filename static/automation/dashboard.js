"use strict";

(() => {
  const form = document.getElementById("automation-form");
  const prompt = document.getElementById("id_input_text");
  const kind = document.getElementById("id_kind");
  const count = document.getElementById("character-count");
  const panel = document.getElementById("result-panel");
  const message = document.getElementById("result-message");
  const output = document.getElementById("ai-output");
  const copy = document.getElementById("copy-result");
  const status = document.getElementById("result-status");
  const labels = { QUEUED: "Waiting", RUNNING: "Working", SUCCEEDED: "Completed", FAILED: "Couldn’t complete" };

  prompt.addEventListener("input", () => { count.textContent = `${prompt.value.length.toLocaleString()} / 4,000`; });
  kind.addEventListener("change", () => {
    const draft = kind.value === "DRAFT_ANNOUNCEMENT";
    document.getElementById("composer-title").textContent = draft ? "A clearer message starts here" : "What’s on your mind?";
    document.getElementById("composer-subtitle").textContent = draft ? "Turn your notes into an announcement you can review." : "Get help with accounts, invitations, and group roles.";
    document.getElementById("prompt-label").textContent = draft ? "Announcement details" : "Your question";
    document.getElementById("prompt-help").textContent = draft ? "Include the purpose, audience, date, and any details you have." : "A little context helps us give you a useful answer.";
    document.getElementById("submit-label").textContent = draft ? "Create draft" : "Ask AI";
    document.getElementById("suggestions").hidden = draft;
    document.querySelectorAll("[data-kind]").forEach(link => {
      const active = link.dataset.kind === kind.value;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  });
  kind.dispatchEvent(new Event("change"));
  document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => {
    prompt.value = button.dataset.prompt;
    prompt.dispatchEvent(new Event("input"));
    prompt.focus();
  }));
  form.addEventListener("submit", () => {
    document.getElementById("submit-button").disabled = true;
    document.getElementById("submit-label").textContent = "Saving…";
  });
  window.addEventListener("pageshow", () => {
    if (document.querySelector(".availability.unavailable")) return;
    document.getElementById("submit-button").disabled = false;
    kind.dispatchEvent(new Event("change"));
  });
  if (copy) copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(output.textContent);
      copy.textContent = "Copied";
      setTimeout(() => { copy.textContent = "Copy text"; }, 2500);
    } catch {
      message.textContent = "Select the result text to copy it.";
    }
  });

  if (!panel.dataset.pollUrl || ["SUCCEEDED", "FAILED"].includes(panel.dataset.status)) return;
  const pollUrl = new URL(panel.dataset.pollUrl, window.location.origin);
  if (pollUrl.origin !== window.location.origin) return;
  let timer = null;
  let pending = false;
  let polls = 0;
  let failures = 0;
  let finished = false;

  function schedule() {
    if (finished || document.hidden || polls >= 60) return;
    clearTimeout(timer);
    timer = setTimeout(poll, 5000);
  }

  async function poll() {
    if (finished || pending || document.hidden) return;
    pending = true;
    polls += 1;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(pollUrl, { credentials: "same-origin", cache: "no-store", signal: controller.signal, headers: { Accept: "application/json" } });
      if ([401, 403].includes(response.status)) {
        message.textContent = "Your session has ended. Sign in again to view your result.";
        finished = true;
        return;
      }
      if (response.status === 404) {
        message.textContent = "This result is no longer available.";
        finished = true;
        return;
      }
      if (!response.ok) throw new Error("Result unavailable");
      const body = await response.json();
      const job = body.data;
      if (!job || !Object.hasOwn(labels, job.status)) throw new Error("Invalid result");
      failures = 0;
      panel.dataset.status = job.status;
      status.textContent = labels[job.status];
      status.dataset.status = job.status;
      const historyStatus = document.getElementById("history-selected-status");
      if (historyStatus) { historyStatus.textContent = labels[job.status]; historyStatus.dataset.status = job.status; }
      if (job.status === "SUCCEEDED") {
        output.textContent = job.output_text;
        output.hidden = false;
        copy.disabled = false;
        message.textContent = "Your result is ready.";
        finished = true;
      } else if (job.status === "FAILED") {
        message.textContent = "We couldn’t complete this request. Please try again later.";
        finished = true;
      } else {
        message.textContent = job.status === "RUNNING" ? "Working on your request. Your result will appear here." : "Your request is saved. We’ll get started shortly.";
      }
    } catch {
      failures += 1;
      message.textContent = "We couldn’t refresh your result. Your request is saved; try refreshing the page.";
      if (failures >= 3) finished = true;
    } finally {
      clearTimeout(timeout);
      pending = false;
      if (!finished && polls >= 60) message.textContent = "This is taking longer than expected. Refresh the page to check again.";
      schedule();
    }
  }
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden && !finished && polls < 60) poll();
  });
  window.addEventListener("pagehide", () => { finished = true; clearTimeout(timer); });
  window.addEventListener("pageshow", event => {
    if (event.persisted && !["SUCCEEDED", "FAILED"].includes(panel.dataset.status)) {
      finished = false;
      polls = 0;
      failures = 0;
      schedule();
    }
  });
  schedule();
})();
