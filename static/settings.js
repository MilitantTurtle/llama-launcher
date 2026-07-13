let settingsToken = "";
let toastTimer;

const el = (id) => document.getElementById(id);

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
  if (settingsToken && options.method === "POST") headers["X-Launcher-Token"] = settingsToken;
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function updatePreview(linkId, value) {
  const address = value.trim();
  el(linkId).href = address.includes("://") ? address : `http://${address}`;
}

async function initializeSettings() {
  try {
    const session = await request("/api/session");
    settingsToken = session.token;
    const settings = await request("/api/settings");
    el("settings-openwebui-enabled").checked = settings.openwebui_enabled;
    el("settings-openwebui").value = settings.openwebui_url;
    el("settings-openterminal").value = settings.openterminal_url;
    el("settings-vane-enabled").checked = settings.vane_enabled;
    el("settings-vane").value = settings.vane_url;
    el("settings-llama-server").value = settings.llama_server_executable;
    el("settings-file").textContent = settings.settings_file;
    updatePreview("settings-openwebui-preview", settings.openwebui_url);
    updatePreview("settings-vane-preview", settings.vane_url);
    el("settings-connection-dot").classList.add("online");
    el("settings-connection-label").textContent = "Online";
  } catch (error) {
    el("settings-connection-label").textContent = "Unavailable";
    toast(error.message, true);
  }
}

el("settings-openwebui").addEventListener("input", (event) => updatePreview("settings-openwebui-preview", event.target.value));
el("settings-vane").addEventListener("input", (event) => updatePreview("settings-vane-preview", event.target.value));
el("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = el("settings-submit");
  submit.disabled = true;
  try {
    const settings = await request("/api/settings", {
      method: "POST",
      body: JSON.stringify({
        openwebui_enabled: el("settings-openwebui-enabled").checked,
        openwebui_url: el("settings-openwebui").value.trim(),
        openterminal_url: el("settings-openterminal").value.trim(),
        vane_enabled: el("settings-vane-enabled").checked,
        vane_url: el("settings-vane").value.trim(),
        llama_server_executable: el("settings-llama-server").value.trim(),
      }),
    });
    el("settings-openwebui-enabled").checked = settings.openwebui_enabled;
    el("settings-openwebui").value = settings.openwebui_url;
    el("settings-openterminal").value = settings.openterminal_url;
    el("settings-vane-enabled").checked = settings.vane_enabled;
    el("settings-vane").value = settings.vane_url;
    el("settings-llama-server").value = settings.llama_server_executable;
    el("settings-file").textContent = settings.settings_file;
    updatePreview("settings-openwebui-preview", settings.openwebui_url);
    updatePreview("settings-vane-preview", settings.vane_url);
    toast("Settings saved");
  } catch (error) {
    toast(error.message, true);
  } finally {
    submit.disabled = false;
  }
});

initializeSettings();
