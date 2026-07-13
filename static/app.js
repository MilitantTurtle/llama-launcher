const state = {
  token: "",
  catalog: [],
  status: { status: "idle" },
  family: "All",
  query: "",
  modelPort: 8000,
  expandedModels: new Set(),
  collapsedActiveModels: new Set(),
  activeModelId: null,
  selectedProfiles: {},
  profileOptions: {},
  profileVision: {},
  profileGeneration: {},
  performanceDefaults: {},
  performanceOptions: {},
  performanceExpanded: new Set(),
  cacheTypes: [],
  services: {},
  servicesEnabled: true,
  vaneEnabled: true,
  resources: {},
};

const el = (id) => document.getElementById(id);
let toastTimer;
let currentPresetDraft = null;
let currentRemoveModel = null;
let currentGroupModel = null;
let currentPresetMatch = null;
let currentPresetMatchKey = "";

const OPTION_FIELDS = [
  { name: "context", label: "Context", integer: true, min: 512, max: 1010000, step: 1 },
  { name: "temperature", label: "Temperature", min: 0, max: 5, step: 0.01 },
  { name: "top_p", label: "Top P", min: 0, max: 1, step: 0.01 },
  { name: "top_k", label: "Top K", integer: true, min: 0, max: 1000, step: 1 },
  { name: "min_p", label: "Min P", min: 0, max: 1, step: 0.01 },
  { name: "presence_penalty", label: "Presence", min: -2, max: 2, step: 0.05 },
  { name: "repeat_penalty", label: "Repeat", min: 0, max: 5, step: 0.05 },
];

function toast(message, isError = false) {
  const node = el("toast");
  node.textContent = message;
  node.classList.toggle("error", isError);
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 3600);
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (state.token && options.method === "POST") headers["X-Launcher-Token"] = state.token;
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function renderServices() {
  const openwebui = state.services.openwebui;
  const openterminal = state.services.openterminal;
  const setServiceState = (dotId, labelId, service) => {
    const live = Boolean(service?.live);
    el(dotId).classList.toggle("online", live);
    el(labelId).textContent = live ? "Connected" : "Disconnected";
  };
  setServiceState("openwebui-dot", "openwebui-state", openwebui);
  setServiceState("openwebui-detail-dot", "openwebui-detail-state", openwebui);
  setServiceState("openterminal-dot", "openterminal-state", openterminal);
  setServiceState("vane-dot", "vane-state", state.services.vane);
  if (openwebui?.open_url) el("openwebui-link").href = openwebui.open_url;
  if (openwebui?.open_url) el("openwebui-direct-link").href = openwebui.open_url;
  if (state.services.vane?.open_url) el("vane-link").href = state.services.vane.open_url;
  document.querySelectorAll(".service-action").forEach((button) => {
    const service = state.services[button.dataset.service];
    button.disabled = button.dataset.action === "start" && Boolean(service?.live);
  });
}

function formatMemory(mib) {
  if (!Number.isFinite(mib)) return "unavailable";
  return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GB` : `${Math.round(mib)} MB`;
}

function renderResources() {
  const vram = state.resources.vram;
  const ram = state.resources.ram;
  const updateMeter = (valueId, barId, trackId, resource) => {
    const percent = resource ? Math.max(0, Math.min(100, Number(resource.percent) || 0)) : 0;
    el(valueId).textContent = resource
      ? `${formatMemory(resource.used_mib)} / ${formatMemory(resource.total_mib)} · ${percent}%`
      : "Unavailable";
    el(barId).style.width = `${percent}%`;
    el(trackId).setAttribute("aria-valuenow", String(percent));
  };
  updateMeter("resource-vram", "resource-vram-bar", "resource-vram-track", vram);
  updateMeter("resource-ram", "resource-ram-bar", "resource-ram-track", ram);
}

async function refreshResources() {
  try {
    state.resources = await request("/api/resources");
  } catch (error) {
    state.resources = {};
  }
  renderResources();
}

async function refreshServices() {
  if (!state.servicesEnabled && !state.vaneEnabled) return;
  try {
    state.services = await request("/api/services");
  } catch (error) {
    state.services = {};
  }
  renderServices();
}

async function controlExternalService(button) {
  const serviceId = button.dataset.service;
  const action = button.dataset.action;
  document.querySelectorAll(".service-action").forEach((item) => { item.disabled = true; });
  const labelId = serviceId === "openwebui" ? "openwebui-detail-state" : "openterminal-state";
  el(labelId).textContent = "Starting…";
  try {
    const result = await request(`/api/services/${serviceId}/${action}`, { method: "POST", body: "{}" });
    state.services[serviceId] = result;
    renderServices();
    toast(`${result.name} ${action} requested`);
    setTimeout(refreshServices, 1200);
    setTimeout(refreshServices, 4000);
  } catch (error) {
    toast(error.message, true);
    await refreshServices();
  }
}

function familyFor(item) {
  return item.family || "Other";
}

function modeIcon(mode) {
  if (mode.includes("Image")) return "◈";
  if (mode.includes("Coding")) return "</>";
  if (mode.includes("Reasoning")) return "R";
  if (mode.includes("Thinking")) return "T";
  if (mode.includes("Instruct")) return "I";
  return "Q";
}

function presetOptions(profile, reset = false) {
  if (reset || !state.profileOptions[profile.id]) {
    state.profileOptions[profile.id] = Object.fromEntries(
      OPTION_FIELDS.map(({ name }) => [name, String(profile.recommended[name])]),
    );
  }
  return state.profileOptions[profile.id];
}

function visionFor(profile, reset = false) {
  if (reset || !(profile.id in state.profileVision)) {
    state.profileVision[profile.id] = Boolean(profile.vision);
  }
  return state.profileVision[profile.id];
}

function optionsDifferFromPreset(profile) {
  const values = presetOptions(profile);
  return generationDiffersFromPreset(profile)
    || visionFor(profile) !== Boolean(profile.vision)
    || OPTION_FIELDS.some(({ name }) => Number(values[name]) !== Number(profile.recommended[name]));
}

function parsedOptions(profile, controls) {
  const values = presetOptions(profile);
  const options = { vision: visionFor(profile) };
  for (const field of OPTION_FIELDS) {
    const input = controls.querySelector(`[name="${field.name}"]`);
    if (!input.reportValidity()) return null;
    const raw = values[field.name].trim();
    if (!raw) {
      toast(`${field.label} is required`, true);
      input.focus();
      return null;
    }
    options[field.name] = field.integer ? Number.parseInt(raw, 10) : Number.parseFloat(raw);
  }
  return options;
}

function generationFor(profile, reset = false) {
  if (reset || !state.profileGeneration[profile.id]) {
    state.profileGeneration[profile.id] = Object.fromEntries(
      Object.entries(profile.generation).map(([name, value]) => [name, String(value)]),
    );
  }
  return state.profileGeneration[profile.id];
}

function generationDiffersFromPreset(profile) {
  const values = generationFor(profile);
  return Object.entries(profile.generation).some(([name, value]) => values[name] !== String(value));
}

function parsedGeneration(profile, controls) {
  const values = generationFor(profile);
  const options = {};
  for (const name of ["n_predict", "reasoning_budget"]) {
    const input = controls.querySelector(`[name="${name}"]`);
    if (!input.reportValidity()) return null;
    options[name] = Number.parseInt(values[name], 10);
  }
  options.reasoning = values.reasoning;
  options.reasoning_preserve = values.reasoning_preserve;
  return options;
}

function performancePreset(profile) {
  return { ...state.performanceDefaults, ...(profile.performance || {}) };
}

function performanceFor(profile, reset = false) {
  if (reset || !state.performanceOptions[profile.id]) {
    state.performanceOptions[profile.id] = Object.fromEntries(
      Object.entries(performancePreset(profile)).map(([name, value]) => [name, String(value)]),
    );
  }
  return state.performanceOptions[profile.id];
}

function performanceDiffersFromPreset(profile) {
  const values = performanceFor(profile);
  return Object.entries(performancePreset(profile)).some(([name, value]) => values[name] !== String(value));
}

function parsedPerformance(profile, controls) {
  const values = performanceFor(profile);
  const integerFields = new Set(["batch_size", "ubatch_size", "parallel", "fit_target"]);
  const options = {};
  for (const [name, rawValue] of Object.entries(values)) {
    const input = controls.querySelector(`[name="${name}"]`);
    if (input && !input.reportValidity()) return null;
    const value = rawValue.trim();
    if (!value) {
      toast(`${name.replaceAll("_", " ")} is required`, true);
      input?.focus();
      return null;
    }
    if (integerFields.has(name)) options[name] = Number.parseInt(value, 10);
    else if (name === "gpu_layers") options[name] = value === "auto" ? "auto" : Number.parseInt(value, 10);
    else options[name] = value;
  }
  if (options.ubatch_size > options.batch_size) {
    toast("Ubatch cannot be greater than batch", true);
    controls.querySelector('[name="ubatch_size"]')?.focus();
    return null;
  }
  return options;
}

function renderFilters() {
  const families = ["All", ...new Set(state.catalog.map(familyFor))];
  el("filters").replaceChildren(...families.map((family) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `filter-button${state.family === family ? " active" : ""}`;
    button.textContent = family;
    button.addEventListener("click", () => {
      state.family = family;
      renderFilters();
      renderCatalog();
    });
    return button;
  }));
}

function modelsFromCatalog() {
  const models = new Map();
  state.catalog.forEach((profile) => {
    if (!models.has(profile.model_id)) {
      models.set(profile.model_id, {
        id: profile.model_id,
        name: profile.group,
        family: familyFor(profile),
        quant: profile.quant,
        projector: profile.projector,
        source: profile.source,
        profiles: [],
      });
    }
    models.get(profile.model_id).profiles.push(profile);
  });
  return [...models.values()];
}

function modelMatches(model) {
  if (state.family !== "All" && model.family !== state.family) return false;
  const profileText = model.profiles.map((profile) => `${profile.name} ${profile.mode}`).join(" ");
  const haystack = `${model.name} ${model.family} ${model.quant} ${profileText}`.toLowerCase();
  return haystack.includes(state.query.toLowerCase());
}

function renderCatalog() {
  const activeModelId = state.status.status === "running" ? (state.status.model_id || null) : null;
  if (activeModelId !== state.activeModelId) {
    if (activeModelId) state.collapsedActiveModels.delete(activeModelId);
    state.activeModelId = activeModelId;
  }
  const visible = modelsFromCatalog().filter(modelMatches);
  const root = el("catalog");
  if (!visible.length) {
    root.innerHTML = '<div class="empty-card">No model matches that filter.</div>';
    return;
  }

  const families = new Map();
  visible.forEach((model) => {
    if (!families.has(model.family)) families.set(model.family, []);
    families.get(model.family).push(model);
  });

  root.replaceChildren(...[...families.entries()].map(([family, models]) => {
    const section = document.createElement("section");
    const heading = document.createElement("div");
    heading.className = "group-heading";
    heading.innerHTML = `<h3>${escapeHtml(family)}</h3><span>${models.length} ${models.length === 1 ? "model" : "models"}</span>`;
    const grid = document.createElement("div");
    grid.className = "model-grid";
    models.forEach((model) => grid.appendChild(modelCard(model)));
    section.append(heading, grid);
    return section;
  }));
}

function modelCard(model) {
  const running = state.status.status === "running";
  const activeProfile = running ? model.profiles.find((profile) => profile.id === state.status.id) : null;
  const active = Boolean(activeProfile);
  const selectedId = activeProfile?.id || state.selectedProfiles[model.id] || model.profiles[0].id;
  const selected = model.profiles.find((profile) => profile.id === selectedId) || model.profiles[0];
  state.selectedProfiles[model.id] = selected.id;
  if (active) state.expandedModels.delete(model.id);
  const expanded = active ? !state.collapsedActiveModels.has(model.id) : state.expandedModels.has(model.id);
  const card = document.createElement("article");
  card.className = `model-card model-summary-card${expanded ? " expanded" : ""}${active ? " active" : ""}`;

  const summary = document.createElement("button");
  summary.type = "button";
  summary.className = "model-card-summary";
  summary.setAttribute("aria-expanded", String(expanded));
  summary.innerHTML = `
    <span class="model-identity">
      <span class="model-mark">${model.family.startsWith("Gemma") ? "G" : "Q"}</span>
      <span class="model-copy">
        <strong>${escapeHtml(model.name)}</strong>
        <small>${escapeHtml(model.quant)} · ${model.profiles.length} ${model.profiles.length === 1 ? "profile" : "profiles"}</small>
      </span>
    </span>
    <span class="model-summary-meta">
      ${model.source === "user" ? '<span class="badge user-badge">USER</span>' : ""}
      ${model.profiles.some((profile) => profile.vision) ? '<span class="badge vision">VISION</span>' : ""}
      <span class="expand-caret" aria-hidden="true">⌄</span>
    </span>`;
  summary.addEventListener("click", () => {
    if (active) {
      if (state.collapsedActiveModels.has(model.id)) {
        state.collapsedActiveModels.delete(model.id);
      } else {
        state.collapsedActiveModels.add(model.id);
      }
    } else if (state.expandedModels.has(model.id)) {
      state.expandedModels.delete(model.id);
    } else {
      state.expandedModels.clear();
      state.expandedModels.add(model.id);
    }
    renderCatalog();
  });
  card.appendChild(summary);

  if (!expanded) return card;

  const panel = document.createElement("div");
  panel.className = "profile-panel";
  const toggles = document.createElement("div");
  toggles.className = "profile-toggle-row";
  toggles.setAttribute("aria-label", `${model.name} profiles`);
  model.profiles.forEach((profile) => {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = `profile-toggle${profile.id === selected.id ? " selected" : ""}`;
    toggle.setAttribute("aria-pressed", String(profile.id === selected.id));
    toggle.innerHTML = `<span>${escapeHtml(modeIcon(profile.mode))}</span>${escapeHtml(profile.name.split(/\s+[\u2014-]\s+/).at(-1))}`;
    toggle.addEventListener("click", () => {
      state.selectedProfiles[model.id] = profile.id;
      presetOptions(profile, true);
      visionFor(profile, true);
      generationFor(profile, true);
      performanceFor(profile, true);
      renderCatalog();
    });
    toggles.appendChild(toggle);
  });

  const projectorControl = document.createElement("div");
  projectorControl.className = `projector-control${visionFor(selected) ? " enabled" : ""}`;
  projectorControl.innerHTML = `
    <div class="projector-copy">
      <span class="section-label">IMAGE PROJECTOR</span>
      <strong title="${escapeHtml(model.projector || "No projector configured")}">${escapeHtml(model.projector || "No projector configured")}</strong>
    </div>`;
  const visionSwitch = document.createElement("label");
  visionSwitch.className = "vision-switch";
  const visionInput = document.createElement("input");
  visionInput.type = "checkbox";
  visionInput.checked = visionFor(selected);
  visionInput.disabled = running || !model.projector;
  visionInput.setAttribute("aria-label", "Enable image input");
  const switchTrack = document.createElement("span");
  switchTrack.className = "switch-track";
  const visionLabel = document.createElement("em");
  visionLabel.textContent = visionInput.checked ? "Image on" : "Image off";
  visionSwitch.append(visionInput, switchTrack, visionLabel);
  visionInput.addEventListener("change", () => {
    state.profileVision[selected.id] = visionInput.checked;
    renderCatalog();
  });
  projectorControl.appendChild(visionSwitch);

  const values = presetOptions(selected);
  const settings = document.createElement("div");
  settings.className = "inline-settings";
  const settingsHeader = document.createElement("div");
  settingsHeader.className = "inline-settings-header";
  settingsHeader.innerHTML = `
    <div>
      <span class="section-label">LAUNCH SETTINGS</span>
      <small>${visionFor(selected) ? "Image input enabled" : "Text only"} · edit any value before launch</small>
    </div>`;
  const presetState = document.createElement("span");
  presetState.className = "preset-state";
  const updatePresetState = () => {
    const changed = optionsDifferFromPreset(selected);
    presetState.textContent = changed ? "Edited" : `${selected.name.split(/\s+[\u2014-]\s+/).at(-1)} preset`;
    presetState.classList.toggle("edited", changed);
  };
  updatePresetState();
  settingsHeader.appendChild(presetState);

  const controls = document.createElement("div");
  controls.className = "inline-settings-grid";
  OPTION_FIELDS.forEach((field) => {
    const label = document.createElement("label");
    label.textContent = field.label;
    const input = document.createElement("input");
    input.name = field.name;
    input.type = "number";
    input.min = String(field.min);
    input.max = String(field.max);
    input.step = String(field.step);
    input.required = true;
    input.disabled = running;
    input.value = values[field.name];
    input.addEventListener("input", () => {
      values[field.name] = input.value;
      updatePresetState();
    });
    label.appendChild(input);
    controls.appendChild(label);
  });
  settings.append(settingsHeader, controls);

  const generationValues = generationFor(selected);
  const generationPanel = document.createElement("div");
  generationPanel.className = "generation-settings";
  generationPanel.innerHTML = `
    <div class="generation-settings-header">
      <span class="section-label">REASONING &amp; OUTPUT</span>
      <small>Reasoning history support depends on the model's chat template.</small>
    </div>`;
  const generationControls = document.createElement("div");
  generationControls.className = "generation-settings-grid";
  const addGenerationNumber = (name, labelText, min, max) => {
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.name = name;
    input.type = "number";
    input.min = String(min);
    input.max = String(max);
    input.step = "1";
    input.required = true;
    input.disabled = running;
    input.value = generationValues[name];
    input.addEventListener("input", () => {
      generationValues[name] = input.value;
      updatePresetState();
    });
    label.appendChild(input);
    generationControls.appendChild(label);
  };
  const addGenerationSelect = (name, labelText, choices) => {
    const label = document.createElement("label");
    label.textContent = labelText;
    const select = document.createElement("select");
    select.name = name;
    select.disabled = running;
    choices.forEach(([value, text]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      select.appendChild(option);
    });
    select.value = generationValues[name];
    select.addEventListener("change", () => {
      generationValues[name] = select.value;
      updatePresetState();
    });
    label.appendChild(select);
    generationControls.appendChild(label);
  };
  addGenerationNumber("n_predict", "Output tokens (-1 = unlimited)", -1, 1010000);
  addGenerationSelect("reasoning", "Reasoning mode", [["auto", "Template default"], ["on", "On"], ["off", "Off"]]);
  addGenerationNumber("reasoning_budget", "Reasoning budget (-1 = unlimited)", -1, 1010000);
  addGenerationSelect("reasoning_preserve", "Reasoning in history", [["auto", "Template default"], ["on", "Preserve thoughts"], ["off", "Final answers only"]]);
  generationPanel.appendChild(generationControls);

  const performanceValues = performanceFor(selected);
  const performancePanel = document.createElement("details");
  performancePanel.className = "performance-panel";
  performancePanel.open = state.performanceExpanded.has(model.id);
  performancePanel.addEventListener("toggle", () => {
    if (performancePanel.open) state.performanceExpanded.add(model.id);
    else state.performanceExpanded.delete(model.id);
  });
  const performanceSummary = document.createElement("summary");
  performanceSummary.innerHTML = `
    <span>
      <span class="section-label">PERFORMANCE</span>
      <small class="performance-overview"></small>
    </span>`;
  const performanceState = document.createElement("span");
  performanceState.className = "preset-state performance-state";
  performanceSummary.appendChild(performanceState);
  const performanceControls = document.createElement("div");
  performanceControls.className = "performance-grid";

  const updatePerformanceState = () => {
    const edited = performanceDiffersFromPreset(selected);
    performanceState.textContent = edited ? "Edited" : (selected.performance ? "Saved preset" : "Server defaults");
    performanceState.classList.toggle("edited", edited);
    performanceSummary.querySelector(".performance-overview").textContent =
      `K ${performanceValues.cache_type_k} · V ${performanceValues.cache_type_v} · ${performanceValues.batch_size}/${performanceValues.ubatch_size} batch · ${performanceValues.parallel} slot${performanceValues.parallel === "1" ? "" : "s"}`;
  };

  const addSelect = (name, labelText, choices) => {
    const label = document.createElement("label");
    label.textContent = labelText;
    const select = document.createElement("select");
    select.name = name;
    select.disabled = running;
    choices.forEach((choice) => {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice;
      select.appendChild(option);
    });
    select.value = performanceValues[name];
    select.addEventListener("change", () => {
      performanceValues[name] = select.value;
      updatePerformanceState();
    });
    label.appendChild(select);
    performanceControls.appendChild(label);
  };

  const addNumber = (name, labelText, min, max) => {
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.name = name;
    input.type = "number";
    input.min = String(min);
    input.max = String(max);
    input.step = "1";
    input.required = true;
    input.disabled = running;
    input.value = performanceValues[name];
    input.addEventListener("input", () => {
      performanceValues[name] = input.value;
      updatePerformanceState();
    });
    label.appendChild(input);
    performanceControls.appendChild(label);
    return input;
  };

  addSelect("cache_type_k", "K cache", state.cacheTypes);
  addSelect("cache_type_v", "V cache", state.cacheTypes);
  addNumber("batch_size", "Batch", 1, 131072);
  addNumber("ubatch_size", "Ubatch", 1, 131072);
  addNumber("parallel", "Parallel slots", 1, 64);

  const fitField = document.createElement("div");
  fitField.className = "performance-toggle-field";
  fitField.innerHTML = '<span>Fit to VRAM</span>';
  const fitSwitch = document.createElement("label");
  fitSwitch.className = "vision-switch compact-switch";
  const fitInput = document.createElement("input");
  fitInput.name = "fit";
  fitInput.type = "checkbox";
  fitInput.checked = performanceValues.fit === "on";
  fitInput.disabled = running;
  fitInput.setAttribute("aria-label", "Fit model to VRAM");
  const fitTrack = document.createElement("span");
  fitTrack.className = "switch-track";
  const fitLabel = document.createElement("em");
  fitLabel.textContent = fitInput.checked ? "On" : "Off";
  fitSwitch.append(fitInput, fitTrack, fitLabel);
  fitField.appendChild(fitSwitch);
  performanceControls.appendChild(fitField);
  const fitTargetInput = addNumber("fit_target", "Fit target MiB", 0, 65536);
  fitTargetInput.disabled = running || !fitInput.checked;
  fitInput.addEventListener("change", () => {
    performanceValues.fit = fitInput.checked ? "on" : "off";
    fitLabel.textContent = fitInput.checked ? "On" : "Off";
    fitTargetInput.disabled = running || !fitInput.checked;
    updatePerformanceState();
  });

  addSelect("flash_attention", "Flash attention", ["on", "off", "auto"]);
  const gpuField = document.createElement("label");
  gpuField.textContent = "GPU layers";
  const gpuInput = document.createElement("input");
  gpuInput.name = "gpu_layers";
  gpuInput.type = "text";
  gpuInput.pattern = "(?:auto|[0-9]{1,4})";
  gpuInput.title = "Enter auto or a whole number from 0 to 1000";
  gpuInput.required = true;
  gpuInput.disabled = running;
  gpuInput.value = performanceValues.gpu_layers;
  gpuInput.addEventListener("input", () => {
    performanceValues.gpu_layers = gpuInput.value.trim().toLowerCase();
    updatePerformanceState();
  });
  gpuField.appendChild(gpuInput);
  performanceControls.appendChild(gpuField);

  const performanceActions = document.createElement("div");
  performanceActions.className = "performance-actions";
  const performanceNote = document.createElement("small");
  performanceNote.textContent = "Reset performance values to this preset's saved defaults.";
  const performanceButtons = document.createElement("div");
  const performanceResetButton = document.createElement("button");
  performanceResetButton.type = "button";
  performanceResetButton.className = "button secondary performance-action-button";
  performanceResetButton.textContent = "Reset";
  performanceResetButton.disabled = running;
  performanceResetButton.addEventListener("click", () => {
    performanceFor(selected, true);
    renderCatalog();
  });
  performanceButtons.appendChild(performanceResetButton);
  performanceActions.append(performanceNote, performanceButtons);
  performanceControls.appendChild(performanceActions);
  updatePerformanceState();
  performancePanel.append(performanceSummary, performanceControls);

  const selection = document.createElement("div");
  selection.className = "selected-profile-summary";
  selection.innerHTML = `
    <div>
      <span class="section-label">SELECTED PROFILE</span>
      <strong>${escapeHtml(selected.name.split(/\s+[\u2014-]\s+/).at(-1))}</strong>
      <small>${visionFor(selected) ? "Image input enabled" : "Text only"} · ${escapeHtml(selected.mode)}</small>
    </div>`;

  const actions = document.createElement("div");
  actions.className = "card-actions";
  const groupModelButton = document.createElement("button");
  groupModelButton.type = "button";
  groupModelButton.className = "button secondary launch-button group-model-button";
  groupModelButton.textContent = "Change group";
  groupModelButton.disabled = running;
  groupModelButton.addEventListener("click", () => openGroupModel(model));
  actions.appendChild(groupModelButton);
  const removeModelButton = document.createElement("button");
  removeModelButton.type = "button";
  removeModelButton.className = "button secondary launch-button remove-model-button";
  removeModelButton.textContent = "Remove model";
  removeModelButton.disabled = running;
  removeModelButton.addEventListener("click", () => openRemoveModel(model));
  actions.appendChild(removeModelButton);
  const savePresetButton = document.createElement("button");
  savePresetButton.type = "button";
  savePresetButton.className = "button secondary launch-button preset-save-button";
  savePresetButton.textContent = "Save preset";
  savePresetButton.disabled = running;
  savePresetButton.addEventListener("click", () => {
    const launchOptions = parsedOptions(selected, controls);
    if (!launchOptions) return;
    const generation = parsedGeneration(selected, generationControls);
    if (!generation) return;
    const performance = parsedPerformance(selected, performanceControls);
    if (!performance) return;
    const sampling = Object.fromEntries(
      OPTION_FIELDS.filter(({ name }) => name !== "context").map(({ name }) => [name, launchOptions[name]]),
    );
    openPresetSave({
      id: selected.id,
      modelId: model.id,
      label: selected.name.split(/\s+[\u2014-]\s+/).at(-1),
      settings: {
        vision: launchOptions.vision,
        context: launchOptions.context,
        sampling,
        generation,
        performance,
      },
    });
  });
  actions.appendChild(savePresetButton);
  const launchButton = document.createElement("button");
  launchButton.type = "button";
  launchButton.className = "button launch-button";
  launchButton.textContent = active && selected.id === state.status.id ? "Running" : "Launch";
  launchButton.disabled = running;
  launchButton.addEventListener("click", () => {
    const options = parsedOptions(selected, controls);
    if (!options) return;
    const generation = parsedGeneration(selected, generationControls);
    if (!generation) return;
    const performance = parsedPerformance(selected, performanceControls);
    if (!performance) return;
    Object.assign(options, generation, performance);
    launch(selected.id, optionsDifferFromPreset(selected) || performanceDiffersFromPreset(selected) ? options : null);
  });
  actions.appendChild(launchButton);
  selection.appendChild(actions);

  const stateLabel = document.createElement("span");
  stateLabel.className = `state-label${active ? " live" : ""}`;
  stateLabel.textContent = active ? "● Live" : "Ready";
  panel.append(toggles, projectorControl, settings, generationPanel, performancePanel, selection, stateLabel);
  card.appendChild(panel);
  return card;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  }[char]));
}

function renderStatus(renderCards = true) {
  const running = state.status.status === "running";
  const apiPort = Number(state.status.external ? (state.status.port || 0) : (state.status.port || state.modelPort));
  el("now-panel").classList.toggle("running", running);
  el("stop-button").classList.toggle("hidden", !running);
  el("api-link").classList.toggle("hidden", !running || !apiPort);
  if (running) {
    const minutes = Math.floor((state.status.elapsed_seconds || 0) / 60);
    const seconds = (state.status.elapsed_seconds || 0) % 60;
    el("now-title").textContent = state.status.name;
    const customLabel = Object.keys(state.status.custom_options || {}).length ? " · custom settings" : "";
    el("now-detail").textContent = `${state.status.mode}${customLabel} · PID ${state.status.pid} · ${minutes}:${String(seconds).padStart(2, "0")} elapsed`;
    el("now-icon").textContent = state.status.vision ? "◈" : "Q";
    const apiHost = location.hostname || "127.0.0.1";
    if (apiPort) el("api-link").href = `http://${apiHost}:${apiPort}/`;
  } else {
    el("now-title").textContent = "No model running";
    const last = state.status.last;
    el("now-detail").textContent = last
      ? `Last: ${last.name} · ${last.status}${Number.isInteger(last.return_code) ? ` (${last.return_code})` : ""}`
      : `Select a launcher below to start llama-server on port ${state.modelPort}.`;
    el("now-icon").textContent = "Q";
  }
  if (renderCards) renderCatalog();
}

async function launch(id, options = null) {
  try {
    const body = options ? { id, options } : { id };
    state.status = await request("/api/launch", { method: "POST", body: JSON.stringify(body) });
    renderStatus();
    await refreshLog();
    toast(options ? "Custom model launch started" : "Model launch started");
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

function openPresetSave(draft) {
  currentPresetDraft = draft;
  el("preset-current-name").textContent = draft.label;
  el("preset-new-name").value = `${draft.label} copy`;
  el("preset-save-modal").classList.remove("hidden");
  el("preset-new-name").focus();
  el("preset-new-name").select();
}

function closePresetSave() {
  currentPresetDraft = null;
  el("preset-save-modal").classList.add("hidden");
  el("preset-new-name").value = "";
}

async function savePreset(action) {
  if (!currentPresetDraft) return;
  const newName = el("preset-new-name").value.trim();
  if (action === "new" && !newName) {
    toast("Enter a name for the new preset", true);
    el("preset-new-name").focus();
    return;
  }
  el("preset-overwrite").disabled = true;
  el("preset-save-new").disabled = true;
  try {
    const draft = currentPresetDraft;
    const result = await request("/api/profiles", {
      method: "POST",
      body: JSON.stringify({
        id: draft.id,
        action,
        name: action === "new" ? newName : undefined,
        settings: draft.settings,
      }),
    });
    state.catalog = result.catalog;
    state.selectedProfiles[draft.modelId] = result.profile.id;
    delete state.profileOptions[result.profile.id];
    delete state.profileVision[result.profile.id];
    delete state.profileGeneration[result.profile.id];
    delete state.performanceOptions[result.profile.id];
    closePresetSave();
    renderCatalog();
    toast(action === "new" ? `Created preset ${newName}` : `Overwrote preset ${draft.label}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    el("preset-overwrite").disabled = false;
    el("preset-save-new").disabled = false;
  }
}

function openRemoveModel(model) {
  currentRemoveModel = model;
  el("remove-model-name").textContent = model.name;
  el("remove-model-modal").classList.remove("hidden");
  el("remove-model-confirm").focus();
}

function closeRemoveModel() {
  currentRemoveModel = null;
  el("remove-model-modal").classList.add("hidden");
}

async function confirmRemoveModel() {
  if (!currentRemoveModel) return;
  const model = currentRemoveModel;
  el("remove-model-confirm").disabled = true;
  try {
    const result = await request("/api/models/remove", {
      method: "POST",
      body: JSON.stringify({ id: model.id }),
    });
    state.catalog = result.catalog;
    state.expandedModels.delete(model.id);
    delete state.selectedProfiles[model.id];
    model.profiles.forEach((profile) => {
      delete state.profileOptions[profile.id];
      delete state.profileVision[profile.id];
      delete state.profileGeneration[profile.id];
      delete state.performanceOptions[profile.id];
    });
    closeRemoveModel();
    renderFilters();
    renderCatalog();
    toast(`${model.name} removed from the launcher; model files were not deleted`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    el("remove-model-confirm").disabled = false;
  }
}

function openGroupModel(model) {
  currentGroupModel = model;
  el("group-model-name").textContent = model.name;
  el("group-model-input").value = model.family;
  el("group-model-modal").classList.remove("hidden");
  el("group-model-input").focus();
  el("group-model-input").select();
}

function closeGroupModel() {
  currentGroupModel = null;
  el("group-model-modal").classList.add("hidden");
}

async function saveModelGroup(event) {
  event.preventDefault();
  if (!currentGroupModel) return;
  const group = el("group-model-input").value.trim();
  if (!group) {
    toast("Enter a group name", true);
    return;
  }
  el("group-model-submit").disabled = true;
  try {
    const model = currentGroupModel;
    const result = await request("/api/models/group", {
      method: "POST",
      body: JSON.stringify({ id: model.id, group }),
    });
    state.catalog = result.catalog;
    state.family = "All";
    closeGroupModel();
    renderFilters();
    renderCatalog();
    toast(`${model.name} moved to ${group}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    el("group-model-submit").disabled = false;
  }
}

function openAddModel() {
  clearPresetMatch();
  el("add-model-form").reset();
  el("add-model-form").elements.namedItem("family").value = "Custom";
  el("add-model-form").elements.namedItem("quant").value = "Custom";
  el("add-model-form").elements.namedItem("profile_name").value = "Default";
  el("add-model-form").elements.namedItem("context").value = "32768";
  el("add-model-form").elements.namedItem("temperature").value = "0.8";
  el("add-model-form").elements.namedItem("top_p").value = "0.95";
  el("add-model-form").elements.namedItem("top_k").value = "40";
  el("add-model-form").elements.namedItem("min_p").value = "0.05";
  el("add-model-form").elements.namedItem("presence_penalty").value = "0";
  el("add-model-form").elements.namedItem("repeat_penalty").value = "1";
  el("add-model-modal").classList.remove("hidden");
  el("add-model-form").elements.namedItem("name").focus();
}

function closeAddModel() {
  el("add-model-modal").classList.add("hidden");
  el("add-model-form").reset();
  clearPresetMatch();
}

function presetMatchKey() {
  const form = el("add-model-form");
  const path = form.elements.namedItem("model_path").value.trim();
  const name = form.elements.namedItem("name").value.trim();
  const projector = form.elements.namedItem("mmproj_path").value.trim();
  return `${path}\n${name}\n${projector}`;
}

function clearPresetMatch() {
  currentPresetMatch = null;
  currentPresetMatchKey = "";
  el("preset-match").classList.add("hidden");
  el("preset-match-profiles").replaceChildren();
}

function presetSettingText(profile) {
  const labels = {
    temperature: "temp",
    top_p: "top-p",
    top_k: "top-k",
    min_p: "min-p",
    presence_penalty: "presence",
    repeat_penalty: "repeat",
    n_predict: "output",
  };
  const values = {...profile.sampling, ...profile.generation};
  const settings = Object.entries(values).map(([key, value]) => `${labels[key] || key} ${value}`);
  if (profile.reasoning) settings.push(`reasoning ${profile.reasoning}`);
  return settings.join(" · ");
}

function renderPresetMatch(match, key) {
  currentPresetMatch = match;
  currentPresetMatchKey = key;
  const panel = el("preset-match");
  if (!match) {
    panel.classList.add("hidden");
    el("preset-match-profiles").replaceChildren();
    return;
  }
  el("preset-match-name").textContent = match.name;
  const source = el("preset-match-source");
  source.href = match.source.url;
  source.title = `${match.source.publisher} · checked ${match.source.checked_at}`;
  const hasProfiles = match.profiles.length > 0;
  const isReference = match.preset_status === "reference";
  el("preset-match-heading").textContent = hasProfiles
    ? isReference ? "CREATOR REFERENCE SETTINGS FOUND" : "CREATOR PRESETS FOUND"
    : "KNOWN MODEL FOUND";
  el("preset-match-choice").classList.toggle("hidden", !hasProfiles);
  el("preset-match-choice-label").textContent = `Add ${match.profiles.length} ${isReference ? "creator-reference" : "creator-recommended"} ${match.profiles.length === 1 ? "profile" : "profiles"}`;
  el("use-preset-match").checked = hasProfiles;
  const profileItems = match.profiles.map((profile) => {
    const item = document.createElement("div");
    item.className = "preset-match-profile";
    const name = document.createElement("strong");
    name.textContent = profile.name;
    const detail = document.createElement("small");
    detail.textContent = presetSettingText(profile);
    item.append(name, detail);
    return item;
  });
  if (!hasProfiles) {
    const item = document.createElement("div");
    item.className = "preset-match-profile";
    const name = document.createElement("strong");
    name.textContent = "No creator sampling preset published";
    const detail = document.createElement("small");
    detail.textContent = "The visible manual values will be kept; the creator source will still be recorded.";
    item.append(name, detail);
    profileItems.push(item);
  }
  el("preset-match-profiles").replaceChildren(...profileItems);
  panel.classList.remove("hidden");
}

async function refreshPresetMatch() {
  const form = el("add-model-form");
  const modelPath = form.elements.namedItem("model_path").value.trim();
  const name = form.elements.namedItem("name").value.trim();
  const mmprojPath = form.elements.namedItem("mmproj_path").value.trim();
  const key = `${modelPath}\n${name}\n${mmprojPath}`;
  if (!modelPath) {
    clearPresetMatch();
    return false;
  }
  try {
    const result = await request(`/api/preset-library/match?model_path=${encodeURIComponent(modelPath)}&name=${encodeURIComponent(name)}&mmproj_path=${encodeURIComponent(mmprojPath)}`);
    if (key !== presetMatchKey()) return false;
    renderPresetMatch(result.match, key);
    return Boolean(result.match);
  } catch (error) {
    if (key === presetMatchKey()) clearPresetMatch();
    toast(error.message, true);
    return false;
  }
}

async function submitAddModel(event) {
  event.preventDefault();
  const formElement = el("add-model-form");
  const form = new FormData(formElement);
  const text = (name) => String(form.get(name) || "").trim();
  const payload = {
    name: text("name"),
    family: text("family"),
    model_path: text("model_path"),
    mmproj_path: text("mmproj_path"),
    alias: text("alias"),
    quant: text("quant"),
    profile_name: text("profile_name"),
    reasoning: text("reasoning"),
    vision: formElement.elements.namedItem("vision").checked,
    no_mmap: formElement.elements.namedItem("no_mmap").checked,
    defaults: {
      context: Number.parseInt(text("context"), 10),
      temperature: Number.parseFloat(text("temperature")),
      top_p: Number.parseFloat(text("top_p")),
      top_k: Number.parseInt(text("top_k"), 10),
      min_p: Number.parseFloat(text("min_p")),
      presence_penalty: Number.parseFloat(text("presence_penalty")),
      repeat_penalty: Number.parseFloat(text("repeat_penalty")),
    },
  };
  el("add-model-submit").disabled = true;
  try {
    if (currentPresetMatchKey !== presetMatchKey()) {
      const found = await refreshPresetMatch();
      if (found) {
        toast(currentPresetMatch?.profiles.length
          ? currentPresetMatch.preset_status === "reference"
            ? "Creator reference settings found — review the choice and submit again"
            : "Creator presets found — review the choice and submit again"
          : "Known model found — no creator sampling preset is published");
        return;
      }
    }
    const matchedProfileCount = currentPresetMatch?.profiles.length || 0;
    const applyingPresetProfiles = matchedProfileCount > 0 && el("use-preset-match").checked;
    if (currentPresetMatch && (applyingPresetProfiles || matchedProfileCount === 0)) payload.preset_id = currentPresetMatch.id;
    const result = await request("/api/models", {method: "POST", body: JSON.stringify(payload)});
    state.catalog = result.catalog;
    state.family = "All";
    renderFilters();
    renderCatalog();
    closeAddModel();
    const addedProfiles = result.catalog.filter((item) => item.model_id === result.model.model_id).length;
    toast(applyingPresetProfiles
      ? currentPresetMatch?.preset_status === "reference"
        ? `${result.model.group} added with ${addedProfiles} creator-reference ${addedProfiles === 1 ? "profile" : "profiles"}`
        : `${result.model.group} added with ${addedProfiles} creator ${addedProfiles === 1 ? "profile" : "profiles"}`
      : payload.preset_id
        ? `${result.model.group} added with creator source recorded and manual settings kept`
        : `${result.model.group} added to the registry`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    el("add-model-submit").disabled = false;
  }
}

async function stopModel() {
  el("stop-button").disabled = true;
  try {
    state.status = await request("/api/stop", { method: "POST", body: "{}" });
    renderStatus();
    await refreshLog();
    toast("Model server stopped");
  } catch (error) {
    toast(error.message, true);
  } finally {
    el("stop-button").disabled = false;
  }
}

async function pollStatus() {
  try {
    const previousStatus = state.status.status;
    const previousId = state.status.id;
    state.status = await request("/api/status");
    renderStatus(previousStatus !== state.status.status || previousId !== state.status.id);
    el("connection-dot").classList.add("online");
    el("connection-label").textContent = "Online";
  } catch (error) {
    el("connection-dot").classList.remove("online");
    el("connection-label").textContent = "Disconnected";
  }
}

async function refreshLog() {
  try {
    const result = await request("/api/log?lines=180");
    el("log-path").textContent = result.file || "No model log yet";
    el("log-output").textContent = result.log || "Model output will appear here after launch.";
    el("log-output").scrollTop = el("log-output").scrollHeight;
  } catch (error) {
    el("log-output").textContent = error.message;
  }
}

function toggleLogPanel() {
  const panel = el("log-panel");
  const toggle = el("log-toggle");
  const collapsed = panel.classList.toggle("collapsed");
  toggle.setAttribute("aria-expanded", String(!collapsed));
  toggle.setAttribute("aria-label", collapsed ? "Show recent output" : "Hide recent output");
}

async function initialize() {
  try {
    const session = await request("/api/session");
    state.token = session.token;
    state.modelPort = session.model_port;
    state.servicesEnabled = session.openwebui_enabled;
    el("service-launcher").classList.toggle("hidden", !state.servicesEnabled);
    el("openwebui-link").href = session.openwebui_url;
    el("openwebui-direct-link").href = session.openwebui_url;
    state.vaneEnabled = session.vane_enabled;
    el("vane-link").classList.toggle("hidden", !state.vaneEnabled);
    el("vane-link").href = session.vane_url;
    state.performanceDefaults = session.performance_defaults;
    state.cacheTypes = session.cache_types;
    el("model-port-note").textContent = `One model at a time · llama-server :${state.modelPort}`;
    state.catalog = await request("/api/catalog");
    state.status = await request("/api/status");
    el("network-note").textContent = `Allowed: ${session.allowed_networks.join(" · ")}`;
    el("connection-dot").classList.add("online");
    el("connection-label").textContent = "Online";
    renderFilters();
    renderStatus();
    await refreshResources();
    await refreshServices();
    await refreshLog();
  } catch (error) {
    el("catalog").innerHTML = `<div class="empty-card">${escapeHtml(error.message)}</div>`;
    el("connection-label").textContent = "Unavailable";
    toast(error.message, true);
  }
}

el("search").addEventListener("input", (event) => {
  state.query = event.target.value.trim();
  renderCatalog();
});
el("services-summary").addEventListener("click", () => {
  const expanded = el("service-launcher").classList.toggle("expanded");
  el("services-summary").setAttribute("aria-expanded", String(expanded));
  document.querySelector(".hero").classList.toggle("services-open", expanded);
});
document.querySelectorAll(".service-action").forEach((button) => {
  button.addEventListener("click", () => controlExternalService(button));
});
el("stop-button").addEventListener("click", stopModel);
el("log-toggle").addEventListener("click", toggleLogPanel);
el("refresh-log").addEventListener("click", refreshLog);
el("group-model-form").addEventListener("submit", saveModelGroup);
el("group-model-close").addEventListener("click", closeGroupModel);
el("group-model-cancel").addEventListener("click", closeGroupModel);
el("group-model-modal").addEventListener("click", (event) => {
  if (event.target === el("group-model-modal")) closeGroupModel();
});
el("remove-model-close").addEventListener("click", closeRemoveModel);
el("remove-model-cancel").addEventListener("click", closeRemoveModel);
el("remove-model-confirm").addEventListener("click", confirmRemoveModel);
el("remove-model-modal").addEventListener("click", (event) => {
  if (event.target === el("remove-model-modal")) closeRemoveModel();
});
el("preset-save-close").addEventListener("click", closePresetSave);
el("preset-save-cancel").addEventListener("click", closePresetSave);
el("preset-overwrite").addEventListener("click", () => savePreset("overwrite"));
el("preset-save-new").addEventListener("click", () => savePreset("new"));
el("preset-save-modal").addEventListener("click", (event) => {
  if (event.target === el("preset-save-modal")) closePresetSave();
});
el("add-model-button").addEventListener("click", openAddModel);
el("add-model-form").addEventListener("submit", submitAddModel);
el("add-model-form").elements.namedItem("model_path").addEventListener("change", refreshPresetMatch);
el("add-model-form").elements.namedItem("mmproj_path").addEventListener("change", refreshPresetMatch);
el("add-model-form").elements.namedItem("name").addEventListener("change", refreshPresetMatch);
el("add-model-close").addEventListener("click", closeAddModel);
el("add-model-cancel").addEventListener("click", closeAddModel);
el("add-model-modal").addEventListener("click", (event) => {
  if (event.target === el("add-model-modal")) closeAddModel();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!el("group-model-modal").classList.contains("hidden")) closeGroupModel();
  if (!el("remove-model-modal").classList.contains("hidden")) closeRemoveModel();
  if (!el("preset-save-modal").classList.contains("hidden")) closePresetSave();
  if (!el("add-model-modal").classList.contains("hidden")) closeAddModel();
});

initialize();
setInterval(pollStatus, 2500);
setInterval(refreshServices, 5000);
setInterval(refreshResources, 5000);
setInterval(() => {
  if (state.status.status === "running") refreshLog();
}, 5000);
