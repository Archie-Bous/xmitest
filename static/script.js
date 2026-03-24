/* global state */
let modelSummary = "";

const $ = (id) => document.getElementById(id);

// ── DOM refs ──────────────────────────────────────────────────────────────
const dropZone       = $("drop-zone");
const fileInput      = $("file-input");
const fileInfo       = $("file-info");
const fileNameEl     = $("file-name");
const clearFileBtn   = $("clear-file");
const parseBtn       = $("parse-btn");
const uploadError    = $("upload-error");
const parseLoading   = $("parse-loading");
const summaryCard    = $("summary-card");
const summaryText    = $("summary-text");
const summaryPlaceholder = $("summary-placeholder");
const copySummaryBtn = $("copy-summary-btn");
const statsCard      = $("stats-card");
const statsGrid      = $("stats-grid");
const apiKeyInput    = $("api-key");
const toggleKeyBtn   = $("toggle-key");
const eyeIcon        = $("eye-icon");
const llmModelSel    = $("llm-model");
const questionInput  = $("question-input");
const reviewBtn      = $("review-btn");
const reviewError    = $("review-error");
const reviewLoading  = $("review-loading");
const chatHistory    = $("chat-history");
const chatMessages   = $("chat-messages");
const quickPrompts   = document.querySelectorAll(".chip");

// ── API key toggle ────────────────────────────────────────────────────────
toggleKeyBtn.addEventListener("click", () => {
  if (apiKeyInput.type === "password") {
    apiKeyInput.type = "text";
    eyeIcon.innerHTML = `
      <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
      <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
      <line x1="1" y1="1" x2="23" y2="23"/>`;
  } else {
    apiKeyInput.type = "password";
    eyeIcon.innerHTML = `
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/>`;
  }
});

// ── File drag & drop ──────────────────────────────────────────────────────
dropZone.addEventListener("click", () => fileInput.click());

dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("drag-over");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("drag-over");
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});

fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) setFile(fileInput.files[0]);
});

function setFile(file) {
  fileNameEl.textContent = file.name;
  fileInfo.classList.remove("hidden");
  parseBtn.disabled = false;
  hideError(uploadError);
}

clearFileBtn.addEventListener("click", () => {
  fileInput.value = "";
  fileInfo.classList.add("hidden");
  parseBtn.disabled = true;
  hideError(uploadError);
});

// ── Parse XMI ────────────────────────────────────────────────────────────
parseBtn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  hideError(uploadError);
  parseLoading.classList.remove("hidden");
  parseBtn.disabled = true;
  summaryPlaceholder.classList.remove("hidden");
  summaryText.classList.add("hidden");
  copySummaryBtn.classList.add("hidden");

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch("/upload", { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) {
      showError(uploadError, data.error || "Upload failed");
      return;
    }

    modelSummary = data.summary;
    summaryText.textContent = data.summary;
    summaryText.classList.remove("hidden");
    summaryPlaceholder.classList.add("hidden");
    copySummaryBtn.classList.remove("hidden");
    renderStats(data.model, data.format_info, data.warnings || []);
    statsCard.classList.remove("hidden");
    reviewBtn.disabled = false;
  } catch (err) {
    showError(uploadError, `Network error: ${err.message}`);
  } finally {
    parseLoading.classList.add("hidden");
    parseBtn.disabled = false;
  }
});

function renderStats(model, formatInfo, warnings) {
  const items = [
    { label: "Classes",        value: model.classes?.length ?? 0 },
    { label: "Interfaces",     value: model.interfaces?.length ?? 0 },
    { label: "Enumerations",   value: model.enumerations?.length ?? 0 },
    { label: "Associations",   value: model.associations?.length ?? 0 },
    { label: "Dependencies",   value: model.dependencies?.length ?? 0 },
    { label: "Packages",       value: model.packages?.length ?? 0 },
    { label: "Use Cases",      value: model.use_cases?.length ?? 0 },
    { label: "Actors",         value: model.actors?.length ?? 0 },
    { label: "State Machines", value: model.state_machines?.length ?? 0 },
    { label: "Interactions",   value: model.interactions?.length ?? 0 },
    { label: "Diagrams",       value: model.diagrams?.length ?? 0 },
  ].filter((i) => i.value > 0);

  if (items.length === 0) {
    items.push({ label: "Elements", value: "0" });
  }

  // Format / tool info badge
  const tool = formatInfo?.tool || "";
  const xmiVer = formatInfo?.xmi_version || "";
  const toolBadge = (tool && tool !== "Unknown")
    ? `<div class="format-badge">
         <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" stroke-width="2" stroke-linecap="round"
              stroke-linejoin="round">
           <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
           <line x1="12" y1="16" x2="12.01" y2="16"/>
         </svg>
         <span><strong>${_esc(tool)}</strong> · XMI ${_esc(xmiVer)}</span>
       </div>`
    : "";

  // Stereotypes row
  const stereos = model.stereotypes_used || [];
  const stereoRow = stereos.length
    ? `<div class="stereo-row">
         <span class="stereo-label">Stereotypes:</span>
         ${stereos.map(s => `<span class="chip">&laquo;${_esc(s)}&raquo;</span>`).join("")}
       </div>`
    : "";

  // Diagrams row
  const diags = model.diagrams || [];
  const diagRow = diags.length
    ? `<div class="stereo-row">
         <span class="stereo-label">Diagrams:</span>
         ${diags.map(d => `<span class="chip">${_esc(d.name)}${d.type ? ` [${_esc(d.type)}]` : ""}</span>`).join("")}
       </div>`
    : "";

  // Warnings
  const warningHtml = (warnings || []).length
    ? `<div class="parse-warnings">
         ${warnings.map(w => `<div class="parse-warning">⚠ ${_esc(w)}</div>`).join("")}
       </div>`
    : "";

  statsGrid.innerHTML =
    toolBadge +
    items.map(({ label, value }) =>
      `<div class="stat-item">
         <div class="stat-value">${value}</div>
         <div class="stat-label">${label}</div>
       </div>`
    ).join("") +
    stereoRow + diagRow + warningHtml;
}

function _esc(str) {
  return String(str)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Copy summary ──────────────────────────────────────────────────────────
copySummaryBtn.addEventListener("click", () => {
  if (!modelSummary) return;
  navigator.clipboard.writeText(modelSummary).then(() => {
    copySummaryBtn.textContent = "✓ Copied!";
    setTimeout(() => {
      copySummaryBtn.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round"
             stroke-linejoin="round">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
        </svg>
        Copy`;
    }, 2000);
  });
});

// ── Quick prompts ─────────────────────────────────────────────────────────
quickPrompts.forEach((chip) => {
  chip.addEventListener("click", () => {
    questionInput.value = chip.dataset.prompt;
    questionInput.focus();
  });
});

// ── LLM Review ───────────────────────────────────────────────────────────
reviewBtn.addEventListener("click", sendReview);
questionInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendReview();
});

async function sendReview() {
  const apiKey   = apiKeyInput.value.trim();
  const question = questionInput.value.trim() ||
    "Please provide a comprehensive review of this UML model.";
  const llmModel = llmModelSel.value;

  if (!apiKey) {
    showError(reviewError, "Please enter your OpenAI API key above.");
    return;
  }
  if (!modelSummary) {
    showError(reviewError, "Please upload and parse an XMI file first.");
    return;
  }

  hideError(reviewError);
  reviewLoading.classList.remove("hidden");
  reviewBtn.disabled = true;

  // Add user bubble immediately
  addBubble("user", question);
  questionInput.value = "";

  try {
    const res = await fetch("/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: apiKey,
        summary: modelSummary,
        question,
        model: llmModel,
      }),
    });
    const data = await res.json();

    if (!res.ok) {
      addBubble("ai", `⚠ Error: ${data.error || "Review failed"}`);
      return;
    }

    addBubble("ai", data.answer);
  } catch (err) {
    addBubble("ai", `⚠ Network error: ${err.message}`);
  } finally {
    reviewLoading.classList.add("hidden");
    reviewBtn.disabled = false;
  }
}

function addBubble(role, text) {
  chatHistory.classList.remove("hidden");

  const wrapper = document.createElement("div");
  const label   = document.createElement("div");
  const bubble  = document.createElement("div");

  label.className   = "bubble-label";
  label.textContent = role === "user" ? "You" : "AI Reviewer";

  bubble.className  = `chat-bubble bubble-${role}`;
  bubble.textContent = text;

  wrapper.appendChild(label);
  wrapper.appendChild(bubble);
  chatMessages.appendChild(wrapper);

  // Scroll to latest
  chatMessages.scrollIntoView({ behavior: "smooth", block: "end" });
}

// ── Helpers ───────────────────────────────────────────────────────────────
function showError(el, msg) {
  el.textContent = msg;
  el.classList.remove("hidden");
}
function hideError(el) {
  el.classList.add("hidden");
}
