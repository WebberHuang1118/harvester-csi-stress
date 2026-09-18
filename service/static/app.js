"use strict";

const state = {
  modes: [],
  parameters: [],
  values: {},
  mode: "stress",
  currentJobId: "",
  currentJob: null,
  activeJobId: "",
  restoredJobId: "",
  submitting: false,
  lastSequence: 0,
  activeFilter: "all",
  pollTimer: null,
};

const elements = {
  form: document.querySelector("#configForm"),
  modeGrid: document.querySelector("#modeGrid"),
  parameterGroups: document.querySelector("#parameterGroups"),
  matrixPreview: document.querySelector("#matrixPreview"),
  configTitle: document.querySelector("#configTitle"),
  formError: document.querySelector("#formError"),
  startButton: document.querySelector("#startButton"),
  stopButton: document.querySelector("#stopButton"),
  resetButton: document.querySelector("#resetButton"),
  jobTitle: document.querySelector("#jobTitle"),
  statusBadge: document.querySelector("#statusBadge"),
  jobHistory: document.querySelector("#jobHistory"),
  runSummary: document.querySelector("#runSummary"),
  progressText: document.querySelector("#progressText"),
  progressValue: document.querySelector("#progressValue"),
  runIdText: document.querySelector("#runIdText"),
  startedText: document.querySelector("#startedText"),
  exitCodeText: document.querySelector("#exitCodeText"),
  currentActivity: document.querySelector("#currentActivity"),
  commandPanel: document.querySelector("#commandPanel"),
  commandText: document.querySelector("#commandText"),
  artifactPanel: document.querySelector("#artifactPanel"),
  reportLink: document.querySelector("#reportLink"),
  copyCommandButton: document.querySelector("#copyCommandButton"),
  logViewport: document.querySelector("#logViewport"),
  logList: document.querySelector("#logList"),
  emptyOutput: document.querySelector("#emptyOutput"),
  logSearch: document.querySelector("#logSearch"),
  autoScroll: document.querySelector("#autoScroll"),
  copyLogsButton: document.querySelector("#copyLogsButton"),
  clearLogsButton: document.querySelector("#clearLogsButton"),
  levelFilters: document.querySelector("#levelFilters"),
  toast: document.querySelector("#toast"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let body = {};
  try {
    body = await response.json();
  } catch (_error) {
    body = { error: `Server returned ${response.status}` };
  }
  if (!response.ok) {
    const error = new Error(body.error || `Request failed with status ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => { elements.toast.hidden = true; }, 2600);
}

function showFormError(message = "") {
  elements.formError.textContent = message;
  elements.formError.hidden = !message;
}

function resetValues() {
  state.values = Object.fromEntries(state.parameters.map((parameter) => [parameter.key, parameter.default]));
}

function activeStatus(status) {
  return ["starting", "running", "stopping"].includes(status);
}

function splitShellCommand(command) {
  const tokens = [];
  let token = "";
  let quote = "";
  let tokenStarted = false;

  for (let index = 0; index < command.length; index += 1) {
    const character = command[index];
    if (quote) {
      if (character === quote) {
        quote = "";
      } else if (character === "\\" && quote === '"' && index + 1 < command.length) {
        index += 1;
        token += command[index];
      } else {
        token += character;
      }
      tokenStarted = true;
      continue;
    }

    if (/\s/.test(character)) {
      if (tokenStarted) {
        tokens.push(token);
        token = "";
        tokenStarted = false;
      }
    } else if (character === "'" || character === '"') {
      quote = character;
      tokenStarted = true;
    } else if (character === "\\" && index + 1 < command.length) {
      index += 1;
      token += command[index];
      tokenStarted = true;
    } else {
      token += character;
      tokenStarted = true;
    }
  }
  if (tokenStarted) tokens.push(token);
  return tokens;
}

function configurationFromCommand(job) {
  if (typeof job.command !== "string" || !job.command.trim()) return null;

  const parametersByFlag = new Map();
  for (const parameter of state.parameters) {
    const flag = parameter.key === "cleanup_run_id"
      ? "--cleanup-run"
      : `--${parameter.key.replaceAll("_", "-")}`;
    parametersByFlag.set(flag, parameter);
  }

  const config = { mode: job.mode };
  const tokens = splitShellCommand(job.command);
  let matched = false;
  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token.startsWith("KUBECONFIG=")) {
      config.kubeconfig = token.slice("KUBECONFIG=".length);
      matched = true;
      continue;
    }
    if (token === "--cleanup-all") {
      config.mode = "cleanup_all";
      matched = true;
      continue;
    }

    const separator = token.indexOf("=");
    const flag = separator > 0 ? token.slice(0, separator) : token;
    const parameter = parametersByFlag.get(flag);
    if (!parameter) continue;

    if (parameter.key === "cleanup_run_id") config.mode = "cleanup_run";
    if (parameter.type === "checkbox") {
      config[parameter.key] = true;
      matched = true;
      continue;
    }

    let value;
    if (separator > 0) {
      value = token.slice(separator + 1);
    } else if (index + 1 < tokens.length) {
      index += 1;
      value = tokens[index];
    } else {
      continue;
    }
    config[parameter.key] = parameter.type === "number" && value !== "" ? Number(value) : value;
    matched = true;
  }
  return matched ? config : null;
}

function updateConfigurationLock() {
  const locked = state.submitting || Boolean(state.activeJobId);
  for (const control of elements.modeGrid.querySelectorAll("input")) control.disabled = locked;
  for (const control of elements.parameterGroups.querySelectorAll("input, select")) control.disabled = locked;
  elements.resetButton.disabled = locked;
  elements.startButton.disabled = locked;
  elements.configTitle.textContent = locked ? "Current operation" : "New operation";
  elements.form.classList.toggle("configuration-locked", locked);
  elements.form.setAttribute("aria-busy", String(locked));
}

function restoreJobConfiguration(job) {
  if (state.restoredJobId === job.id) return;
  const config = job.config || configurationFromCommand(job);
  if (!config) return;
  state.mode = config.mode || job.mode;
  for (const parameter of state.parameters) {
    if (Object.prototype.hasOwnProperty.call(config, parameter.key)) {
      state.values[parameter.key] = config[parameter.key];
    }
  }
  state.restoredJobId = job.id;
  renderModes();
  renderParameters();
  updateStartLabel();
}

function syncActiveJob(job = null) {
  if (job && activeStatus(job.status)) {
    state.activeJobId = job.id;
    restoreJobConfiguration(job);
  } else {
    state.activeJobId = "";
    state.restoredJobId = "";
  }
  updateConfigurationLock();
}

function renderModes() {
  elements.modeGrid.replaceChildren();
  for (const mode of state.modes) {
    const wrapper = document.createElement("div");
    wrapper.className = "mode-option";

    const input = document.createElement("input");
    input.type = "radio";
    input.name = "mode";
    input.id = `mode-${mode.value}`;
    input.value = mode.value;
    input.checked = mode.value === state.mode;
    input.disabled = state.submitting || Boolean(state.activeJobId);
    input.addEventListener("change", () => {
      state.mode = mode.value;
      showFormError();
      renderModes();
      renderParameters();
      updateMatrixPreview();
      updateStartLabel();
    });

    const label = document.createElement("label");
    label.htmlFor = input.id;
    const title = document.createElement("span");
    title.className = "mode-label";
    title.textContent = mode.label;
    const description = document.createElement("span");
    description.className = "mode-description";
    description.textContent = mode.description;
    label.append(title, description);
    wrapper.append(input, label);
    elements.modeGrid.append(wrapper);
  }
}

function isWideField(parameter) {
  return ["kubeconfig", "image_url", "network_name", "node_selector", "ssh_identity_file", "cleanup_run_id"].includes(parameter.key);
}

function createField(parameter) {
  const field = document.createElement("div");
  field.className = `field${isWideField(parameter) ? " wide" : ""}`;

  if (parameter.type === "checkbox") {
    field.classList.add("checkbox-field");
    const label = document.createElement("label");
    label.className = "checkbox-label";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.name = parameter.key;
    input.checked = Boolean(state.values[parameter.key]);
    input.disabled = state.submitting || Boolean(state.activeJobId);
    input.addEventListener("change", () => {
      state.values[parameter.key] = input.checked;
      updateMatrixPreview();
    });
    const text = document.createElement("span");
    text.textContent = parameter.label;
    label.append(input, text);
    field.append(label);
    if (parameter.help) field.append(createHelp(parameter.help));
    return field;
  }

  const label = document.createElement("label");
  label.className = "field-label";
  label.htmlFor = `parameter-${parameter.key}`;
  label.textContent = parameter.label;
  if (parameter.required) {
    const required = document.createElement("span");
    required.className = "required-mark";
    required.textContent = "required";
    label.append(required);
  }

  const inputWrap = document.createElement("div");
  inputWrap.className = `input-wrap${parameter.suffix ? " has-suffix" : ""}`;
  const input = document.createElement(parameter.type === "select" ? "select" : "input");
  input.id = `parameter-${parameter.key}`;
  input.name = parameter.key;
  if (parameter.type === "select") {
    for (const optionValue of parameter.options) {
      input.add(new Option(optionValue, optionValue));
    }
  } else {
    input.type = parameter.type;
  }
  input.value = state.values[parameter.key];
  input.disabled = state.submitting || Boolean(state.activeJobId);
  input.required = Boolean(parameter.required);
  if (parameter.min !== undefined) input.min = String(parameter.min);
  if (parameter.max !== undefined) input.max = String(parameter.max);
  if (parameter.type === "number") input.step = "1";
  if (parameter.placeholder) input.placeholder = parameter.placeholder;
  input.addEventListener("input", () => {
    state.values[parameter.key] = input.value;
    updateMatrixPreview();
  });
  inputWrap.append(input);
  if (parameter.suffix) {
    const suffix = document.createElement("span");
    suffix.className = "input-suffix";
    suffix.textContent = parameter.suffix;
    inputWrap.append(suffix);
  }

  field.append(label, inputWrap);
  if (parameter.help) field.append(createHelp(parameter.help));
  return field;
}

function createHelp(text) {
  const help = document.createElement("p");
  help.className = "field-help";
  help.textContent = text;
  return help;
}

function renderParameters() {
  elements.parameterGroups.replaceChildren();
  const activeParameters = state.parameters.filter((parameter) => parameter.modes.includes(state.mode));
  const groups = [...new Set(activeParameters.map((parameter) => parameter.group))];

  for (const groupName of groups) {
    const section = document.createElement("section");
    section.className = "parameter-group";
    const heading = document.createElement("h3");
    heading.className = "group-title";
    heading.textContent = groupName;
    const fields = document.createElement("div");
    fields.className = "fields";
    for (const parameter of activeParameters.filter((candidate) => candidate.group === groupName)) {
      fields.append(createField(parameter));
    }
    section.append(heading, fields);
    elements.parameterGroups.append(section);
  }
  updateMatrixPreview();
}

function positiveInteger(value, fallback = 0) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : fallback;
}

function updateMatrixPreview() {
  if (state.mode !== "benchmark") {
    elements.matrixPreview.hidden = true;
    return;
  }
  const engines = String(state.values.benchmark_engines || "").split(",").filter(Boolean).length;
  const sizes = String(state.values.benchmark_sizes_gib || "").split(",").filter((item) => /^[1-9][0-9]*$/.test(item.trim())).length;
  const paired = positiveInteger(state.values.benchmark_paired_runs);
  const faultRuns = positiveInteger(state.values.benchmark_fault_runs);
  const suites = new Set(String(state.values.benchmark_suites || "").split(","));
  const smoke = state.values.benchmark_profile === "smoke";
  let campaigns = 0;
  let operations = 0;
  if (smoke) {
    campaigns = 1;
    operations = engines * 2;
  } else {
    campaigns += engines;
    operations += engines * 2;
    if (suites.has("capacity")) {
      campaigns += sizes * paired;
      operations += sizes * paired * engines * 2;
    }
    if (suites.has("sensitivity")) {
      campaigns += 2 * paired;
      operations += 2 * paired * engines * 2;
    }
    if (suites.has("incremental")) {
      campaigns += paired;
      operations += paired * 5 * engines * 2;
    }
    if (suites.has("cache")) {
      campaigns += paired;
      operations += paired * engines * 3;
    }
    if (state.values.benchmark_include_fault_recovery) {
      campaigns += engines * 2 * 2 * faultRuns;
      operations += engines * 2 * 2 * faultRuns * 2;
    }
  }
  elements.matrixPreview.textContent = `Planned base workload: ${campaigns} campaigns, ${operations} backup/restore operations. Recovery retries may add operations.`;
  elements.matrixPreview.hidden = false;
}

function updateStartLabel() {
  const labels = {
    stress: ["▶", "Start stress test"],
    benchmark: ["▶", "Start benchmark"],
    cleanup_run: ["◆", "Clean selected run"],
    cleanup_all: ["◆", "Clean all runs"],
  };
  elements.startButton.querySelector(".button-icon").textContent = labels[state.mode][0];
  elements.startButton.lastChild.textContent = ` ${labels[state.mode][1]}`;
}

function currentPayload() {
  return { mode: state.mode, ...state.values };
}

function terminalStatus(status) {
  return ["succeeded", "failed", "stopped"].includes(status);
}

function formatMode(mode) {
  return { stress: "Stress", benchmark: "Benchmark", cleanup_run: "Cleanup run", cleanup_all: "Cleanup all" }[mode] || mode;
}

function formatStarted(timestamp) {
  if (!timestamp) return "—";
  const date = new Date(timestamp);
  return Number.isNaN(date.valueOf()) ? timestamp : date.toLocaleString();
}

function renderJob(job) {
  state.currentJob = job;
  elements.jobTitle.textContent = `${formatMode(job.mode)} · ${job.id}`;
  elements.statusBadge.textContent = job.status;
  elements.statusBadge.className = `status-badge ${job.status}`;
  elements.runSummary.hidden = false;
  elements.commandPanel.hidden = false;
  elements.commandText.textContent = job.command;
  if (job.mode === "benchmark" && job.artifact_dir) {
    elements.reportLink.href = `/api/jobs/${job.id}/artifacts/report.md`;
    elements.artifactPanel.hidden = false;
  } else {
    elements.artifactPanel.hidden = true;
  }
  elements.runIdText.textContent = job.run_id || "Pending";
  elements.startedText.textContent = formatStarted(job.started_at);
  elements.exitCodeText.textContent = job.exit_code === null ? "—" : String(job.exit_code);
  elements.currentActivity.textContent = job.last_message || "Waiting for output…";

  const active = ["starting", "running", "stopping"].includes(job.status);
  elements.stopButton.disabled = !active;
  elements.copyLogsButton.disabled = false;
  if (job.id === state.activeJobId && !active) syncActiveJob();
  updateConfigurationLock();

  if (job.total_iterations > 0) {
    const completed = Math.min(job.completed_iterations, job.total_iterations);
    const percentage = Math.round((completed / job.total_iterations) * 100);
    elements.progressText.textContent = `${completed} of ${job.total_iterations} iterations`;
    elements.progressValue.classList.remove("indeterminate");
    elements.progressValue.style.width = `${percentage}%`;
  } else if (active) {
    elements.progressText.textContent = job.status === "stopping" ? "Stopping" : "In progress";
    elements.progressValue.style.width = "35%";
    elements.progressValue.classList.add("indeterminate");
  } else {
    elements.progressText.textContent = job.status;
    elements.progressValue.classList.remove("indeterminate");
    elements.progressValue.style.width = job.status === "succeeded" ? "100%" : "0%";
  }
}

function resetJobView() {
  window.clearInterval(state.pollTimer);
  state.pollTimer = null;
  state.currentJobId = "";
  state.currentJob = null;
  state.lastSequence = 0;
  elements.jobTitle.textContent = "No run selected";
  elements.statusBadge.textContent = "Idle";
  elements.statusBadge.className = "status-badge idle";
  elements.runSummary.hidden = true;
  elements.commandPanel.hidden = true;
  elements.artifactPanel.hidden = true;
  elements.stopButton.disabled = true;
  elements.copyLogsButton.disabled = true;
  elements.logList.replaceChildren();
  elements.emptyOutput.hidden = false;
  updateConfigurationLock();
}

function appendLogs(logs) {
  if (!logs.length) return;
  elements.emptyOutput.hidden = true;
  const fragment = document.createDocumentFragment();
  for (const log of logs) {
    state.lastSequence = Math.max(state.lastSequence, log.sequence);
    const row = document.createElement("div");
    row.className = "log-entry";
    row.dataset.level = log.level;
    row.dataset.search = `${log.context} ${log.message}`.toLowerCase();

    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = log.timestamp.includes("T") ? log.timestamp.slice(11, 19) : log.timestamp;
    const context = document.createElement("span");
    context.className = "log-context";
    context.textContent = log.context || "process";
    context.title = log.context || "process";
    const message = document.createElement("span");
    message.className = "log-message";
    message.textContent = log.message;
    row.append(time, context, message);
    fragment.append(row);
  }
  elements.logList.append(fragment);
  applyLogFilters();
  if (elements.autoScroll.checked) elements.logViewport.scrollTop = elements.logViewport.scrollHeight;
}

function applyLogFilters() {
  const search = elements.logSearch.value.trim().toLowerCase();
  for (const row of elements.logList.children) {
    let levelMatches = state.activeFilter === "all" || row.dataset.level === state.activeFilter;
    if (state.activeFilter === "activity") {
      levelMatches = ["activity", "success", "checksum", "system"].includes(row.dataset.level);
    }
    const searchMatches = !search || row.dataset.search.includes(search);
    row.classList.toggle("hidden", !(levelMatches && searchMatches));
  }
}

async function refreshCurrentJob() {
  if (!state.currentJobId) return;
  try {
    const body = await api(`/api/jobs/${state.currentJobId}/logs?after=${state.lastSequence}`);
    renderJob(body.job);
    appendLogs(body.logs);
    if (terminalStatus(body.job.status)) {
      await refreshHistory(false);
      if (!state.activeJobId) {
        window.clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
    }
  } catch (error) {
    if (error.status === 404) {
      resetJobView();
      await refreshHistory(false);
      showToast("The service restarted; previous in-memory run history was cleared");
      return;
    }
    showToast(error.message);
  }
}

async function refreshActiveJob() {
  if (!state.activeJobId || state.activeJobId === state.currentJobId) return;
  try {
    const body = await api(`/api/jobs/${state.activeJobId}`);
    if (activeStatus(body.job.status)) {
      syncActiveJob(body.job);
    } else {
      await refreshHistory(false);
    }
  } catch (error) {
    if (error.status === 404) {
      await refreshHistory(false);
      return;
    }
    showToast(error.message);
  }
}

async function pollInterface() {
  await refreshCurrentJob();
  await refreshActiveJob();
  if (!state.activeJobId && state.currentJob && terminalStatus(state.currentJob.status)) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function startPolling() {
  window.clearInterval(state.pollTimer);
  state.pollTimer = window.setInterval(pollInterface, 1000);
}

async function selectJob(jobId) {
  if (!jobId) return;
  state.currentJobId = jobId;
  state.lastSequence = 0;
  elements.logList.replaceChildren();
  elements.emptyOutput.hidden = false;
  elements.jobHistory.value = jobId;
  await refreshCurrentJob();
  if (state.activeJobId || (state.currentJob && !terminalStatus(state.currentJob.status))) startPolling();
}

async function refreshHistory(selectNewest = false) {
  const body = await api("/api/jobs");
  const previous = state.currentJobId;
  const activeJob = body.jobs.find((job) => activeStatus(job.status)) || null;
  syncActiveJob(activeJob);
  elements.jobHistory.replaceChildren();
  if (!body.jobs.length) {
    const option = new Option("No runs yet", "");
    elements.jobHistory.add(option);
    return;
  }
  for (const job of body.jobs) {
    const label = `${formatMode(job.mode)} · ${job.id} · ${job.status}`;
    elements.jobHistory.add(new Option(label, job.id));
  }
  if (selectNewest || !previous) {
    await selectJob(activeJob ? activeJob.id : body.jobs[0].id);
  } else if (body.jobs.some((job) => job.id === previous)) {
    elements.jobHistory.value = previous;
  }
}

async function submitOperation(event) {
  event.preventDefault();
  showFormError();
  if (!elements.form.reportValidity()) return;

  const payload = currentPayload();
  if (state.mode === "cleanup_all") {
    const confirmed = window.confirm(
      `Delete all detected stress and benchmark resources in namespace "${payload.namespace}"? ` +
      "This includes retained VM backups and may delete their remote backup data."
    );
    if (!confirmed) return;
    payload.confirm_cleanup = true;
  } else if (state.mode === "cleanup_run" && payload.cleanup_run_id.startsWith("vmbr-")) {
    const confirmed = window.confirm(
      `Delete benchmark resources for "${payload.cleanup_run_id}"? ` +
      "This may delete remote backup data."
    );
    if (!confirmed) return;
  }

  state.submitting = true;
  updateConfigurationLock();
  try {
    const body = await api("/api/jobs", { method: "POST", body: JSON.stringify(payload) });
    await refreshHistory(false);
    await selectJob(body.job.id);
    showToast("Operation started");
  } catch (error) {
    showFormError(error.message);
  } finally {
    state.submitting = false;
    updateConfigurationLock();
  }
}

async function stopCurrentJob() {
  if (!state.currentJobId || !state.currentJob) return;
  if (!window.confirm("Stop this operation? The script will attempt to clean up resources before exiting.")) return;
  elements.stopButton.disabled = true;
  try {
    const body = await api(`/api/jobs/${state.currentJobId}/stop`, { method: "POST", body: "{}" });
    renderJob(body.job);
    showToast("Stop requested");
  } catch (error) {
    showToast(error.message);
  }
}

async function writeClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_clipboardError) {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.append(textarea);
    textarea.select();
    try {
      return document.execCommand("copy");
    } catch (_fallbackError) {
      return false;
    } finally {
      textarea.remove();
    }
  }
}

async function copyCommand() {
  if (!state.currentJob) return;
  const copied = await writeClipboard(state.currentJob.command);
  showToast(copied ? "Command copied" : "Clipboard access is unavailable");
}

async function copyAllOutput() {
  if (!state.currentJobId) return;
  elements.copyLogsButton.disabled = true;
  try {
    const body = await api(`/api/jobs/${state.currentJobId}/logs?after=0`);
    if (!body.logs.length) {
      showToast("This run has no output to copy");
      return;
    }
    const output = body.logs.map((log) => {
      const context = log.context ? ` [${log.context}]` : "";
      return `[${log.timestamp}]${context} ${log.message}`;
    }).join("\n");
    const copied = await writeClipboard(output);
    showToast(copied ? `Copied ${body.logs.length} output lines` : "Clipboard access is unavailable");
  } catch (error) {
    showToast(error.message);
  } finally {
    elements.copyLogsButton.disabled = !state.currentJobId;
  }
}

async function initialize() {
  try {
    const config = await api("/api/config");
    state.modes = config.modes;
    state.parameters = config.parameters;
    resetValues();
    renderModes();
    renderParameters();
    updateMatrixPreview();
    updateStartLabel();
    await refreshHistory(true);
  } catch (error) {
    showFormError(`Unable to initialize the interface: ${error.message}`);
  }
}

elements.form.addEventListener("submit", submitOperation);
elements.stopButton.addEventListener("click", stopCurrentJob);
elements.copyCommandButton.addEventListener("click", copyCommand);
elements.copyLogsButton.addEventListener("click", copyAllOutput);
elements.resetButton.addEventListener("click", () => {
  if (state.activeJobId || state.submitting) return;
  state.mode = "stress";
  resetValues();
  renderModes();
  renderParameters();
  updateStartLabel();
  showFormError();
});
elements.jobHistory.addEventListener("change", () => selectJob(elements.jobHistory.value));
elements.logSearch.addEventListener("input", applyLogFilters);
elements.clearLogsButton.addEventListener("click", () => {
  elements.logList.replaceChildren();
  elements.emptyOutput.hidden = false;
});
elements.levelFilters.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  state.activeFilter = button.dataset.filter;
  for (const candidate of elements.levelFilters.querySelectorAll("button")) {
    candidate.classList.toggle("active", candidate === button);
  }
  applyLogFilters();
});

initialize();
