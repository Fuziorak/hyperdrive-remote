(() => {
  const STORAGE_KEY = "hyperdrive.profiles.v1";
  const SLOTS_KEY = "hyperdrive.slots.v1";

  const BUILTIN_PROFILES = [
    {
      id: "builtin-course",
      name: "Course",
      builtin: true,
      settings: {
        velocity: 500,
        acceleration: 1200,
        lane: 0,
        lightPreset: "green",
      },
    },
    {
      id: "builtin-red",
      name: "Feux rouge",
      builtin: true,
      settings: {
        velocity: 0,
        acceleration: 1000,
        lane: 0,
        lightPreset: "red",
      },
    },
    {
      id: "builtin-flag",
      name: "Flag queue flash",
      builtin: true,
      settings: {
        velocity: 0,
        acceleration: 1000,
        lane: 0,
        lightPreset: "tailFlash",
      },
    },
  ];

  const LIGHT_PRESET_BTNS = [
    { id: "off", label: "Off", cls: "" },
    { id: "green", label: "Vert", cls: "green" },
    { id: "red", label: "Rouge", cls: "red" },
    { id: "blue", label: "Bleu", cls: "blue" },
    { id: "police", label: "Police", cls: "police" },
    { id: "tailFlash", label: "Queue flash ★", cls: "flash" },
    { id: "strobe", label: "Strobe", cls: "strobe" },
  ];

  const state = {
    vehicles: {},
    /** Ordre fixe d'apparition dans le garage (ne pas trier par lastSeen). */
    garageOrder: [],
    meta: null,
    mqttConnected: false,
    hold: {},
    slots: { A: null, B: null },
    selectedId: null,
    autoBootDone: false,
    autoScanned: false,
    /** Conduite clavier WASD sur la piste active. */
    manualDrive: false,
    manualSlot: "A",
    race: {
      running: false,
      startTs: 0,
      lapStartTs: 0,
      bestMs: null,
      laps: [],
      raf: null,
    },
  };

  const $ = (id) => document.getElementById(id);
  const logEl = $("log");
  const toastEl = $("toast");

  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { toastEl.hidden = true; }, 2400);
  }

  function logLine(kind, text) {
    const line = document.createElement("div");
    line.className = kind;
    line.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
    logEl.prepend(line);
    while (logEl.childElementCount > 200) logEl.lastChild.remove();
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || res.statusText || "Erreur");
    }
    return data;
  }

  function dbg(level, message, data = {}) {
    // Fire-and-forget UI breadcrumb into debug.log
    fetch("/api/debug/client", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ level, message, data }),
    }).catch(() => {});
  }

  function cockpitEl(slot) {
    return document.querySelector(`.cockpit[data-slot="${slot}"]`);
  }

  function assignedIds() {
    return [state.slots.A, state.slots.B].filter(Boolean);
  }

  function saveSlots() {
    localStorage.setItem(SLOTS_KEY, JSON.stringify(state.slots));
  }

  function loadSlots() {
    try {
      const raw = JSON.parse(localStorage.getItem(SLOTS_KEY) || "{}");
      // Ignore leftover fake simulation IDs from older builds
      const isFake = (id) => /^SIM/i.test(String(id || ""));
      if (raw.A && !isFake(raw.A)) state.slots.A = raw.A;
      if (raw.B && !isFake(raw.B)) state.slots.B = raw.B;
      if (isFake(raw.A) || isFake(raw.B)) saveSlots();
    } catch {
      /* ignore */
    }
  }

  function loadCustomProfiles() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    } catch {
      return [];
    }
  }

  function saveCustomProfiles(list) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
  }

  function allProfiles() {
    return [...BUILTIN_PROFILES, ...loadCustomProfiles()];
  }

  function formatMs(ms) {
    if (ms == null || Number.isNaN(ms)) return "—";
    const total = Math.max(0, ms);
    const m = Math.floor(total / 60000);
    const s = Math.floor((total % 60000) / 1000);
    const cs = Math.floor((total % 1000) / 10);
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(cs).padStart(2, "0")}`;
  }

  // ---------------- fleet / slots ----------------

  function syncCockpitLayout() {
    const root = $("cockpits");
    const hasB = !!state.slots.B;
    root.classList.toggle("single", !hasB);
    cockpitEl("B").hidden = !hasB;
  }

  function assignSlot(slot, vehicleId, { quiet } = {}) {
    if (state.slots.A === vehicleId) state.slots.A = null;
    if (state.slots.B === vehicleId) state.slots.B = null;
    state.slots[slot] = vehicleId;
    state.selectedId = vehicleId;
    saveSlots();
    renderVehicles();
    refreshCockpit("A");
    refreshCockpit("B");
    syncCockpitLayout();
    if (!quiet) toast(`Véhicule → piste ${slot}`);
  }

  function clearSlot(slot) {
    if (state.slots[slot] && state.selectedId === state.slots[slot]) {
      state.selectedId = state.slots.A || state.slots.B || null;
    }
    state.slots[slot] = null;
    saveSlots();
    renderVehicles();
    refreshCockpit("A");
    refreshCockpit("B");
    syncCockpitLayout();
  }

  function updateHero() {
    const hero = $("selectedHero");
    const id = state.selectedId || state.slots.A;
    const v = (id && state.vehicles[id]) || null;
    if (!v) {
      $("heroTitle").textContent = "Aucun";
      $("heroMeta").textContent = "Clique un véhicule dans la liste pour commencer";
      hero.classList.remove("active");
      return;
    }
    $("heroTitle").textContent = v.model || "Véhicule";
    const bits = [
      id,
      v.connected ? "connecté" : (v.status || "détecté"),
      v.batteryLevel != null ? `${v.batteryLevel}%` : null,
      v.speed != null ? `${v.speed} mm/s` : null,
    ].filter(Boolean);
    $("heroMeta").textContent = bits.join(" · ");
    hero.classList.remove("active");
    void hero.offsetWidth;
    hero.classList.add("active");
  }

  function pickSlotForVehicle(vehicleId) {
    if (state.slots.A === vehicleId) return "A";
    if (state.slots.B === vehicleId) return "B";
    if (!state.slots.A) return "A";
    if (!state.slots.B) return "B";
    // Deux voitures déjà assignées : un 3e clic remplace la piste A.
    return "A";
  }

  async function selectVehicleForControl(vehicleId, preferredSlot) {
    const slot = preferredSlot || pickSlotForVehicle(vehicleId);
    state.selectedId = vehicleId;
    assignSlot(slot, vehicleId, { quiet: true });
    updateHero();
    try {
      await api(`/api/vehicles/${encodeURIComponent(vehicleId)}/connect`, {
        method: "POST",
        body: JSON.stringify({ enable: true }),
      });
      toast(`Piste ${slot} · connecté — commande à droite`);
    } catch (err) {
      toast(err.message);
    }
  }

  function rememberGarageOrder(id) {
    if (!id || state.garageOrder.includes(id)) return;
    state.garageOrder.push(id);
  }

  function orderedVehicleIds() {
    // Keep first-seen order; append any missing ids (e.g. from snapshot) stably by id.
    Object.keys(state.vehicles).forEach(rememberGarageOrder);
    return state.garageOrder.filter((id) => state.vehicles[id]);
  }

  function renderVehicles() {
    const list = $("vehicleList");
    const ids = orderedVehicleIds();
    if (!ids.length) {
      list.innerHTML = `<p class="empty">Aucun véhicule détecté pour l’instant…</p>`;
      updateHero();
      return;
    }
    list.innerHTML = "";
    ids.forEach((id) => {
      const v = state.vehicles[id];
      const onA = state.slots.A === id;
      const onB = state.slots.B === id;
      const selected = state.selectedId === id || onA;
      const row = document.createElement("div");
      row.className = `vehicle-item${onA || onB ? " active" : ""}${selected ? " selected-control" : ""}`;
      row.dataset.vehicleId = id;
      const slotLabel = onA ? " · piste A" : onB ? " · piste B" : "";
      row.innerHTML = `
          <div class="vehicle-thumb" aria-hidden="true"></div>
          <div>
            <strong>${escapeHtml(v.model || "Véhicule")}</strong>
            <span>${escapeHtml(id)}${slotLabel}${v.connected ? "" : " · à connecter"}</span>
          </div>
          <div class="assign-btns">
            <span class="badge ${v.connected ? "on" : ""}">${v.connected ? "connecté" : (v.status || "vu")}</span>
            <button type="button" class="btn ghost tiny" data-assign="A" title="Piste A">A</button>
            <button type="button" class="btn ghost tiny" data-assign="B" title="Piste B">B</button>
          </div>
        `;
      row.addEventListener("click", () => {
        selectVehicleForControl(id);
      });
      row.querySelector('[data-assign="A"]').addEventListener("click", (e) => {
        e.stopPropagation();
        selectVehicleForControl(id, "A");
      });
      row.querySelector('[data-assign="B"]').addEventListener("click", (e) => {
        e.stopPropagation();
        selectVehicleForControl(id, "B");
      });
      list.appendChild(row);
    });
    updateHero();
  }

  /** Update badge/classes in place — never reshuffle garage on telemetry. */
  function patchVehicleRow(id) {
    const row = document.querySelector(`#vehicleList .vehicle-item[data-vehicle-id="${CSS.escape(id)}"]`);
    if (!row) {
      renderVehicles();
      return;
    }
    const v = state.vehicles[id] || {};
    const onA = state.slots.A === id;
    const onB = state.slots.B === id;
    const selected = state.selectedId === id || onA;
    row.className = `vehicle-item${onA || onB ? " active" : ""}${selected ? " selected-control" : ""}`;
    const badge = row.querySelector(".badge");
    if (badge) {
      badge.className = `badge ${v.connected ? "on" : ""}`;
      badge.textContent = v.connected ? "connecté" : (v.status || "vu");
    }
    const span = row.querySelector("div > span");
    if (span) {
      span.textContent = `${id}${selected ? " · sélectionné" : ""}${onB && !onA ? " · piste B" : ""}`;
    }
  }

  function updateTelemetry(slot) {
    const root = cockpitEl(slot);
    const id = state.slots[slot];
    const v = (id && state.vehicles[id]) || {};
    root.querySelector(".t-status").textContent = v.status || (v.connected ? "connected" : "—");
    root.querySelector(".t-battery").textContent = v.batteryLevel != null ? `${v.batteryLevel}%` : "—";
    root.querySelector(".t-speed").textContent = v.speed != null ? String(v.speed) : "—";
    root.querySelector(".t-track").textContent = v.onTrack == null ? "—" : (v.onTrack ? "oui" : "non");
    root.querySelector(".t-rssi").textContent = v.rssi != null ? String(v.rssi) : "—";
  }

  function refreshCockpit(slot) {
    const root = cockpitEl(slot);
    const id = state.slots[slot];
    const v = (id && state.vehicles[id]) || null;
    root.querySelector(".cockpit-title").textContent = v ? (v.model || "Véhicule") : "Libre";
    root.querySelector(".cockpit-id").textContent = id
      ? `${id}${v && v.connected ? " · prêt" : " · connexion…"}`
      : slot === "A"
        ? "Clique un véhicule dans le garage"
        : "2e véhicule → piste B";
    root.classList.toggle("empty-slot", !id);
    root.querySelector(".hold-toggle-input").checked = !!(id && state.hold[id]);
    updateTelemetry(slot);
    if (slot === "A") updateHero();
    syncCockpitLayout();
  }

  function requireSlot(slot) {
    const id = state.slots[slot];
    if (!id) {
      toast(`Piste ${slot} vide`);
      throw new Error("no vehicle");
    }
    return id;
  }

  function collectLights(root) {
    const lights = {};
    root.querySelectorAll(".lights-grid .light-row").forEach((row) => {
      lights[row.dataset.channel] = {
        effect: row.querySelector('[data-field="effect"]').value,
        start: 0,
        end: 15,
        frequency: Number(row.querySelector('[data-field="frequency"]').value || 0),
      };
    });
    return lights;
  }

  function readCockpitSettings(slot) {
    const root = cockpitEl(slot);
    return {
      velocity: Number(root.querySelector(".speed-slider").value),
      acceleration: Number(root.querySelector(".accel-input").value || 1000),
      lane: Number(root.querySelector(".lane-slider").value),
      lights: collectLights(root),
    };
  }

  function applySettingsToCockpitUI(slot, settings) {
    const root = cockpitEl(slot);
    if (settings.velocity != null) {
      root.querySelector(".speed-slider").value = settings.velocity;
      root.querySelector(".speed-value").textContent = String(settings.velocity);
    }
    if (settings.acceleration != null) {
      root.querySelector(".accel-input").value = settings.acceleration;
    }
    if (settings.lane != null) {
      root.querySelector(".lane-slider").value = settings.lane;
      root.querySelector(".lane-value").textContent = String(settings.lane);
    }
  }

  async function sendSpeed(slot, { quiet } = {}) {
    const id = requireSlot(slot);
    const root = cockpitEl(slot);
    const velocity = Number(root.querySelector(".speed-slider").value);
    const acceleration = Number(root.querySelector(".accel-input").value || 1000);
    await api(`/api/vehicles/${encodeURIComponent(id)}/speed`, {
      method: "POST",
      body: JSON.stringify({ velocity, acceleration }),
    });
    if (!quiet) toast(`Piste ${slot}: vitesse ${velocity}`);
  }

  async function sendLane(slot) {
    const id = requireSlot(slot);
    const root = cockpitEl(slot);
    await api(`/api/vehicles/${encodeURIComponent(id)}/lane`, {
      method: "POST",
      body: JSON.stringify({
        offset: Number(root.querySelector(".lane-slider").value),
        velocity: Math.max(0, Number(root.querySelector(".speed-slider").value) || 300),
        acceleration: 800,
      }),
    });
    toast(`Piste ${slot}: lane`);
  }

  // ---------------- manual WASD drive ----------------

  const MANUAL_DEFAULTS = { velocity: 400, lane: 0, acceleration: 1000 };
  const MANUAL_SPEED_STEP = 50;
  const MANUAL_LANE_STEP = 17;

  function activeManualSlot() {
    if (state.manualSlot === "B" && state.slots.B) return "B";
    if (state.slots.A) return "A";
    if (state.slots.B) return "B";
    return null;
  }

  function setManualDrive(on) {
    state.manualDrive = !!on;
    const btn = $("btnManualDrive");
    const hint = $("manualDriveHint");
    if (btn) btn.setAttribute("aria-pressed", state.manualDrive ? "true" : "false");
    if (btn) btn.textContent = state.manualDrive ? "Clavier ON" : "Clavier WASD";
    if (hint) hint.hidden = !state.manualDrive;
    if (state.manualDrive) {
      state.manualSlot = state.slots.A ? "A" : state.slots.B ? "B" : "A";
      toast(`WASD → piste ${state.manualSlot}`);
    } else {
      toast("Clavier OFF");
    }
  }

  function clamp(n, lo, hi) {
    return Math.max(lo, Math.min(hi, n));
  }

  async function manualAdjustSpeed(delta) {
    const slot = activeManualSlot();
    if (!slot) {
      toast("Assigne un véhicule d’abord");
      return;
    }
    const root = cockpitEl(slot);
    const slider = root.querySelector(".speed-slider");
    const next = clamp(Number(slider.value) + delta, Number(slider.min), Number(slider.max));
    slider.value = String(next);
    root.querySelector(".speed-value").textContent = String(next);
    await sendSpeed(slot, { quiet: true });
    logLine("ok", `WASD vitesse ${next} (piste ${slot})`);
  }

  async function manualAdjustLane(delta) {
    const slot = activeManualSlot();
    if (!slot) {
      toast("Assigne un véhicule d’abord");
      return;
    }
    const root = cockpitEl(slot);
    const slider = root.querySelector(".lane-slider");
    const next = clamp(Number(slider.value) + delta, Number(slider.min), Number(slider.max));
    slider.value = String(next);
    root.querySelector(".lane-value").textContent = String(next);
    await sendLane(slot);
    logLine("ok", `WASD voie offset ${next} (piste ${slot})`);
  }

  async function manualResetDefaults() {
    const slot = activeManualSlot();
    if (!slot) {
      toast("Assigne un véhicule d’abord");
      return;
    }
    const root = cockpitEl(slot);
    root.querySelector(".speed-slider").value = String(MANUAL_DEFAULTS.velocity);
    root.querySelector(".speed-value").textContent = String(MANUAL_DEFAULTS.velocity);
    root.querySelector(".accel-input").value = String(MANUAL_DEFAULTS.acceleration);
    root.querySelector(".lane-slider").value = String(MANUAL_DEFAULTS.lane);
    root.querySelector(".lane-value").textContent = String(MANUAL_DEFAULTS.lane);
    await sendSpeed(slot, { quiet: true });
    await sendLane(slot);
    toast(`Défauts piste ${slot}: ${MANUAL_DEFAULTS.velocity} mm/s · offset 0`);
    logLine("ok", `WASD reset défauts piste ${slot}`);
  }

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
  }

  document.addEventListener("keydown", (ev) => {
    if (!state.manualDrive) return;
    if (isTypingTarget(ev.target)) return;
    const key = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
    if (key === "1") {
      state.manualSlot = "A";
      toast("Clavier → piste A");
      return;
    }
    if (key === "2") {
      state.manualSlot = "B";
      toast("Clavier → piste B");
      return;
    }
    const map = {
      w: () => manualAdjustSpeed(MANUAL_SPEED_STEP),
      s: () => manualAdjustSpeed(-MANUAL_SPEED_STEP),
      a: () => manualAdjustLane(-MANUAL_LANE_STEP),
      d: () => manualAdjustLane(MANUAL_LANE_STEP),
      " ": () => manualResetDefaults(),
      Spacebar: () => manualResetDefaults(),
    };
    const fn = map[key] || (ev.code === "Space" ? map[" "] : null);
    if (!fn) return;
    ev.preventDefault();
    fn().catch((err) => {
      if (err.message !== "no vehicle") toast(err.message);
    });
  });

  async function sendLightsPreset(slot, preset) {
    const id = requireSlot(slot);
    await api(`/api/vehicles/${encodeURIComponent(id)}/lights`, {
      method: "POST",
      body: JSON.stringify({ preset }),
    });
    toast(`Piste ${slot}: feux ${preset}`);
  }

  async function sendLightsCustom(slot) {
    const id = requireSlot(slot);
    const root = cockpitEl(slot);
    await api(`/api/vehicles/${encodeURIComponent(id)}/lights`, {
      method: "POST",
      body: JSON.stringify({ lights: collectLights(root) }),
    });
    toast(`Piste ${slot}: feux envoyés`);
  }

  async function applyProfileToSlot(profile, slot) {
    const id = requireSlot(slot);
    const s = profile.settings || {};
    applySettingsToCockpitUI(slot, s);

    if (s.velocity != null) {
      await api(`/api/vehicles/${encodeURIComponent(id)}/speed`, {
        method: "POST",
        body: JSON.stringify({
          velocity: s.velocity,
          acceleration: s.acceleration ?? 1000,
        }),
      });
    }
    if (s.lane != null) {
      await api(`/api/vehicles/${encodeURIComponent(id)}/lane`, {
        method: "POST",
        body: JSON.stringify({
          offset: s.lane,
          velocity: Math.max(0, s.velocity || 300),
          acceleration: 800,
        }),
      });
    }
    if (s.lightPreset) {
      await api(`/api/vehicles/${encodeURIComponent(id)}/lights`, {
        method: "POST",
        body: JSON.stringify({ preset: s.lightPreset }),
      });
    } else if (s.lights) {
      await api(`/api/vehicles/${encodeURIComponent(id)}/lights`, {
        method: "POST",
        body: JSON.stringify({ lights: s.lights }),
      });
    }
  }

  // ---------------- profiles UI ----------------

  function renderProfiles() {
    const box = $("profileList");
    box.innerHTML = "";
    allProfiles().forEach((p) => {
      const row = document.createElement("div");
      row.className = "profile-item";
      row.innerHTML = `
        <div>
          <strong>${escapeHtml(p.name)}</strong>
          <span>${p.builtin ? "intégré" : "perso"} · v=${p.settings?.velocity ?? "?"} · ${p.settings?.lightPreset || "feux custom"}</span>
        </div>
        <div class="profile-actions">
          <button type="button" class="btn ghost tiny" data-to="A">→ A</button>
          <button type="button" class="btn ghost tiny" data-to="B">→ B</button>
          <button type="button" class="btn accent tiny" data-to="both">A+B</button>
          ${p.builtin ? "" : '<button type="button" class="btn ghost tiny" data-del="1">✕</button>'}
        </div>
      `;
      row.querySelector('[data-to="A"]').addEventListener("click", async () => {
        try {
          await applyProfileToSlot(p, "A");
          toast(`Profil « ${p.name} » → A`);
        } catch (err) {
          if (err.message !== "no vehicle") toast(err.message);
        }
      });
      row.querySelector('[data-to="B"]').addEventListener("click", async () => {
        try {
          await applyProfileToSlot(p, "B");
          toast(`Profil « ${p.name} » → B`);
        } catch (err) {
          if (err.message !== "no vehicle") toast(err.message);
        }
      });
      row.querySelector('[data-to="both"]').addEventListener("click", async () => {
        try {
          if (state.slots.A) await applyProfileToSlot(p, "A");
          if (state.slots.B) await applyProfileToSlot(p, "B");
          if (!state.slots.A && !state.slots.B) toast("Aucune piste assignée");
          else toast(`Profil « ${p.name} » → A+B`);
        } catch (err) {
          if (err.message !== "no vehicle") toast(err.message);
        }
      });
      const del = row.querySelector("[data-del]");
      if (del) {
        del.addEventListener("click", () => {
          const next = loadCustomProfiles().filter((x) => x.id !== p.id);
          saveCustomProfiles(next);
          renderProfiles();
          toast("Profil supprimé");
        });
      }
      box.appendChild(row);
    });
  }

  // ---------------- race ----------------

  function updateRaceUI() {
    const now = performance.now();
    const total = state.race.running ? now - state.race.startTs : (state.race.startTs ? state.race._frozenTotal || 0 : 0);
    const lap = state.race.running ? now - state.race.lapStartTs : (state.race._frozenLap || 0);
    $("raceTimer").textContent = formatMs(state.race.running ? total : (state.race._frozenTotal || 0));
    $("raceLapCurrent").textContent = state.race.running || state.race.laps.length ? formatMs(state.race.running ? lap : state.race._frozenLap || 0) : "—";
    $("raceBest").textContent = formatMs(state.race.bestMs);
    $("raceLapCount").textContent = String(state.race.laps.length);
    const list = $("raceLapList");
    list.innerHTML = state.race.laps
      .map((ms, i) => `<div class="lap-row${ms === state.race.bestMs ? " best" : ""}"><span>Tour ${i + 1}</span><strong>${formatMs(ms)}</strong></div>`)
      .join("");
  }

  function raceTick() {
    if (!state.race.running) return;
    updateRaceUI();
    state.race.raf = requestAnimationFrame(raceTick);
  }

  function raceStart() {
    const now = performance.now();
    state.race.running = true;
    state.race.startTs = now;
    state.race.lapStartTs = now;
    state.race._frozenTotal = 0;
    state.race._frozenLap = 0;
    cancelAnimationFrame(state.race.raf);
    state.race.raf = requestAnimationFrame(raceTick);
    toast("Départ !");
  }

  function raceLap() {
    if (!state.race.running) {
      toast("Lance le chrono d'abord");
      return;
    }
    const now = performance.now();
    const lapMs = now - state.race.lapStartTs;
    state.race.laps.push(lapMs);
    if (state.race.bestMs == null || lapMs < state.race.bestMs) {
      state.race.bestMs = lapMs;
    }
    state.race.lapStartTs = now;
    updateRaceUI();
    toast(`Tour ${state.race.laps.length}: ${formatMs(lapMs)}`);
  }

  function raceStop() {
    if (!state.race.running) return;
    const now = performance.now();
    state.race._frozenTotal = now - state.race.startTs;
    state.race._frozenLap = now - state.race.lapStartTs;
    state.race.running = false;
    cancelAnimationFrame(state.race.raf);
    updateRaceUI();
  }

  function raceReset() {
    state.race.running = false;
    cancelAnimationFrame(state.race.raf);
    state.race.startTs = 0;
    state.race.lapStartTs = 0;
    state.race.bestMs = null;
    state.race.laps = [];
    state.race._frozenTotal = 0;
    state.race._frozenLap = 0;
    updateRaceUI();
  }

  async function panicAll() {
    const ids = assignedIds();
    if (!ids.length) {
      toast("Aucun véhicule assigné");
      return;
    }
    raceStop();
    await Promise.all(
      ids.map((id) =>
        api(`/api/vehicles/${encodeURIComponent(id)}/panic`, {
          method: "POST",
          body: "{}",
        }).catch((err) => {
          logLine("err", err.message);
        })
      )
    );
    ["A", "B"].forEach((slot) => {
      const root = cockpitEl(slot);
      root.querySelector(".speed-slider").value = 0;
      root.querySelector(".speed-value").textContent = "0";
    });
    toast("STOP ALL");
  }

  // ---------------- cockpit wiring ----------------

  function buildLightsGrid(root) {
    const channels = state.meta?.lightChannels || [
      "frontGreen", "frontRed", "tail", "engineRed", "engineGreen", "engineBlue",
    ];
    const effects = state.meta?.lightEffects || [
      "off", "steady", "fade", "pulse", "flash", "strobe",
    ];
    const grid = root.querySelector(".lights-grid");
    grid.innerHTML = "";
    channels.forEach((ch) => {
      const row = document.createElement("div");
      row.className = "light-row";
      row.dataset.channel = ch;
      row.innerHTML = `
        <div class="name">${ch}</div>
        <select data-field="effect">
          ${effects.map((e) => `<option value="${e}">${e}</option>`).join("")}
        </select>
        <input data-field="frequency" type="number" min="0" max="255" value="10" title="fréquence" />
      `;
      grid.appendChild(row);
    });
  }

  function wireCockpit(slot) {
    const root = cockpitEl(slot);
    const speedPresets = root.querySelector(".speed-presets");
    speedPresets.innerHTML = [0, 200, 400, 600, 800]
      .map((v) => `<button class="btn ghost" type="button" data-v="${v}">${v}</button>`)
      .join("");

    const lightPresets = root.querySelector(".light-presets");
    lightPresets.innerHTML = LIGHT_PRESET_BTNS
      .map((p) => `<button class="preset ${p.cls}" type="button" data-preset="${p.id}">${p.label}</button>`)
      .join("");

    buildLightsGrid(root);

    const speedSlider = root.querySelector(".speed-slider");
    const speedValue = root.querySelector(".speed-value");
    let speedTimer = null;
    speedSlider.addEventListener("input", () => {
      speedValue.textContent = speedSlider.value;
      clearTimeout(speedTimer);
      speedTimer = setTimeout(() => {
        sendSpeed(slot).catch((err) => {
          if (err.message !== "no vehicle") toast(err.message);
        });
      }, 180);
    });

    speedPresets.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        speedSlider.value = btn.dataset.v;
        speedValue.textContent = btn.dataset.v;
        sendSpeed(slot).catch((err) => {
          if (err.message !== "no vehicle") toast(err.message);
        });
      });
    });

    const laneSlider = root.querySelector(".lane-slider");
    laneSlider.addEventListener("input", () => {
      root.querySelector(".lane-value").textContent = laneSlider.value;
    });

    root.querySelector(".btn-lane").addEventListener("click", () => {
      sendLane(slot).catch((err) => {
        if (err.message !== "no vehicle") toast(err.message);
      });
    });
    root.querySelector(".btn-lane-cancel").addEventListener("click", async () => {
      try {
        const id = requireSlot(slot);
        await api(`/api/vehicles/${encodeURIComponent(id)}/cancel-lane`, { method: "POST", body: "{}" });
      } catch (err) {
        if (err.message !== "no vehicle") toast(err.message);
      }
    });

    lightPresets.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        sendLightsPreset(slot, btn.dataset.preset).catch((err) => {
          if (err.message !== "no vehicle") toast(err.message);
        });
      });
    });
    root.querySelector(".btn-lights").addEventListener("click", () => {
      sendLightsCustom(slot).catch((err) => {
        if (err.message !== "no vehicle") toast(err.message);
      });
    });

    root.querySelector(".btn-connect").addEventListener("click", async () => {
      try {
        const id = requireSlot(slot);
        await api(`/api/vehicles/${encodeURIComponent(id)}/connect`, {
          method: "POST",
          body: JSON.stringify({ enable: true }),
        });
        toast(`Connect ${slot}`);
      } catch (err) {
        if (err.message !== "no vehicle") toast(err.message);
      }
    });
    root.querySelector(".btn-disconnect").addEventListener("click", async () => {
      try {
        const id = requireSlot(slot);
        await api(`/api/vehicles/${encodeURIComponent(id)}/connect`, {
          method: "POST",
          body: JSON.stringify({ enable: false }),
        });
      } catch (err) {
        if (err.message !== "no vehicle") toast(err.message);
      }
    });
    root.querySelector(".btn-assert").addEventListener("click", async () => {
      try {
        const id = requireSlot(slot);
        await api(`/api/vehicles/${encodeURIComponent(id)}/assert`, { method: "POST", body: "{}" });
        toast(`Réappliqué ${slot}`);
      } catch (err) {
        if (err.message !== "no vehicle") toast(err.message);
      }
    });
    root.querySelector(".btn-panic").addEventListener("click", async () => {
      try {
        const id = requireSlot(slot);
        root.querySelector(".speed-slider").value = 0;
        root.querySelector(".speed-value").textContent = "0";
        await api(`/api/vehicles/${encodeURIComponent(id)}/panic`, { method: "POST", body: "{}" });
        toast(`STOP ${slot}`);
      } catch (err) {
        if (err.message !== "no vehicle") toast(err.message);
      }
    });
    root.querySelector(".btn-clear-slot").addEventListener("click", () => clearSlot(slot));

    root.querySelector(".hold-toggle-input").addEventListener("change", async (e) => {
      try {
        const id = requireSlot(slot);
        const enable = e.target.checked;
        const data = await api(`/api/vehicles/${encodeURIComponent(id)}/hold`, {
          method: "POST",
          body: JSON.stringify({ enable, interval: 1.5 }),
        });
        applySnapshot(data.state);
        toast(enable ? `Maintien ${slot} ON` : `Maintien ${slot} OFF`);
      } catch (err) {
        e.target.checked = false;
        if (err.message !== "no vehicle") toast(err.message);
      }
    });
  }

  // ---------------- mqtt / snapshot ----------------

  function applySnapshot(snap) {
    if (!snap) return;
    state.mqttConnected = !!snap.mqttConnected;
    state.vehicles = snap.vehicles || {};
    Object.keys(state.vehicles).forEach(rememberGarageOrder);
    state.hold = snap.hold || {};
    $("mqttPill").dataset.state = state.mqttConnected ? "on" : "off";
    $("mqttLabel").textContent = state.mqttConnected
      ? `MQTT · ${snap.broker}`
      : "MQTT déconnecté";
    $("scanLabel").textContent = snap.scanning ? "Scan en cours" : "Scan off";
    if (snap.hostId) $("hostId").value = snap.hostId;
    if (snap.mqttUsername != null) $("mqttUsername").value = snap.mqttUsername;
    if (snap.callerId) $("callerIdLabel").textContent = snap.callerId;
    renderVehicles();
    refreshCockpit("A");
    refreshCockpit("B");
  }

  async function startAutoScan() {
    try {
      await api("/api/discover", { method: "POST", body: JSON.stringify({ enable: true }) });
      $("scanLabel").textContent = "Scan en cours";
      logLine("in", "Scan automatique des véhicules");
    } catch (err) {
      logLine("err", `Scan auto: ${err.message}`);
    }
  }

  async function waitForMqtt(timeoutMs = 10000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      try {
        const snap = await api("/api/state");
        applySnapshot(snap);
        if (snap.mqttConnected) return true;
      } catch {
        /* ignore */
      }
      await new Promise((r) => setTimeout(r, 400));
    }
    return false;
  }

  async function connectBrokerRequest({ quiet, force = false } = {}) {
    const data = await api("/api/broker/connect", {
      method: "POST",
      body: JSON.stringify({
        brokerHost: $("brokerHost").value.trim() || "10.42.0.1",
        brokerPort: Number($("brokerPort").value) || 1883,
        hostId: $("hostId").value.trim() || "hyperdrive",
        mqttUsername: $("mqttUsername").value.trim(),
        mqttPassword: $("mqttPassword").value,
        force,
      }),
    });
    applySnapshot(data.state);
    if (!quiet && data.brokerReachable === false) {
      $("mqttPill").dataset.state = "err";
      $("mqttLabel").textContent = "Hors Wi‑Fi hyperdrive";
      logLine("err", "TCP 10.42.0.1:1883 inaccessible — reconnecte le Wi‑Fi SSID hyperdrive");
      toast("Pas sur le Wi‑Fi hyperdrive");
    }
    return data;
  }

  async function autoBoot() {
    if (state.autoBootDone) return;
    state.autoBootDone = true;
    toast("Connexion au lab…");
    dbg("INFO", "autoBoot start");

    // Probe TCP first so the UI explains Wi‑Fi issues clearly.
    try {
      const ping = await api("/api/broker/ping");
      if (ping.reachable === false) {
        $("mqttPill").dataset.state = "err";
        $("mqttLabel").textContent = "Hors Wi‑Fi hyperdrive";
        toast("Wi‑Fi hyperdrive requis");
        logLine("err", "Broker 10.42.0.1 injoignable — IP actuelle hors réseau lab");
        dbg("ERROR", "autoBoot: broker TCP unreachable");
      }
    } catch {
      /* ignore */
    }

    try {
      await connectBrokerRequest({ quiet: false });
    } catch (err) {
      logLine("err", `Auto MQTT: ${err.message}`);
      dbg("ERROR", "autoBoot connect error", { error: err.message });
    }

    const ok = await waitForMqtt(20000);
    if (ok) {
      state.autoScanned = true;
      toast("Lab connecté · scan…");
      dbg("INFO", "autoBoot MQTT OK, starting scan");
      await startAutoScan();
      return;
    }

    toast("MQTT pas prêt — reste sur hyperdrive puis Reconnecter MQTT");
    dbg("ERROR", "autoBoot failed after wait");
    logLine(
      "err",
      "Toujours hors broker. Vérifie SSID hyperdrive (IP 10.42.0.x) puis Reconnecter MQTT"
    );
  }

  function handleEvent(msg) {
    switch (msg.type) {
      case "hello":
        applySnapshot(msg.state);
        break;
      case "mqttConnected":
        state.mqttConnected = true;
        $("mqttPill").dataset.state = "on";
        $("mqttLabel").textContent = `MQTT · ${msg.broker}`;
        logLine("in", `Connecté au broker ${msg.broker}`);
        toast("MQTT connecté");
        // Si la connexion arrive après le timeout auto, on scanne quand même
        if (!state.autoScanned) {
          state.autoScanned = true;
          startAutoScan();
        }
        break;
      case "mqttDisconnected":
        state.mqttConnected = false;
        $("mqttPill").dataset.state = "off";
        $("mqttLabel").textContent = "MQTT déconnecté · reconnexion…";
        logLine("err", `MQTT déconnecté (${msg.reason || "?"}) — reconnexion auto serveur`);
        // Ne pas force-reconnect côté UI : ça tuait le handshake (hier = test_mqtt OK).
        break;
      case "mqttError":
        $("mqttPill").dataset.state = "err";
        logLine("err", msg.message || "Erreur MQTT");
        toast(msg.message || "Erreur MQTT");
        break;
      case "scanning":
        $("scanLabel").textContent = msg.value ? "Scan en cours" : "Scan off";
        break;
      case "vehicleDiscovered": {
        const id = msg.vehicleId;
        const isNew = !state.vehicles[id];
        state.vehicles[id] = {
          ...(state.vehicles[id] || {}),
          id,
          model: msg.model || state.vehicles[id]?.model,
          rssi: msg.rssi ?? state.vehicles[id]?.rssi,
          lastSeen: Date.now() / 1000,
        };
        rememberGarageOrder(id);
        if (isNew) {
          renderVehicles();
          logLine("in", `Véhicule découvert: ${id} (${msg.model || "?"})`);
        } else {
          patchVehicleRow(id);
        }
        break;
      }
      case "vehicleUpdate": {
        const id = msg.vehicleId;
        const isNew = !state.vehicles[id];
        state.vehicles[id] = {
          ...(state.vehicles[id] || { id }),
          ...msg.state,
          lastSeen: Date.now() / 1000,
        };
        rememberGarageOrder(id);
        if (isNew) renderVehicles();
        else patchVehicleRow(id);
        if (state.slots.A === id) updateTelemetry("A");
        if (state.slots.B === id) updateTelemetry("B");
        if (state.selectedId === id || state.slots.A === id) updateHero();
        break;
      }
      case "mqttOut":
        logLine("out", `→ ${msg.topic} ${JSON.stringify(msg.payload)}`);
        break;
      case "mqttIn":
        if (String(msg.topic).includes("/E/") || String(msg.topic).includes("discovered")) {
          logLine("in", `← ${msg.topic}`);
        }
        break;
      case "subscription":
        logLine("in", `Subscriptions dynamiques pour ${msg.vehicleId}`);
        break;
      case "assertControl":
        logLine("out", `Réassert contrôle ${msg.vehicleId}`);
        break;
      case "holdChanged":
        state.hold[msg.vehicleId] = !!msg.enabled;
        refreshCockpit("A");
        refreshCockpit("B");
        break;
      default:
        break;
    }
  }

  function connectWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => {
      logLine("ok", "WebSocket connecté");
    };
    ws.onmessage = (ev) => {
      try {
        handleEvent(JSON.parse(ev.data));
      } catch {
        /* ignore */
      }
    };
    ws.onclose = () => {
      logLine("err", "WebSocket fermé — reconnexion…");
      setTimeout(connectWs, 1500);
    };
  }

  /** Backup sync when WS events were missed (e.g. MQTT up before page JS ran). */
  function startStatePoll() {
    setInterval(async () => {
      try {
        applySnapshot(await api("/api/state"));
      } catch {
        /* server down / tab closing */
      }
    }, 2500);
  }

  // ---------------- global buttons ----------------

  $("btnConnect").addEventListener("click", async () => {
    try {
      toast("Reconnexion MQTT…");
      // Manual button may force only if broker is unreachable
      const data = await connectBrokerRequest({ force: true });
      const ok = await waitForMqtt(20000);
      if (ok) {
        state.autoScanned = true;
        await startAutoScan();
        toast("MQTT OK · scan lancé");
      } else if (data.brokerReachable === false) {
        toast("Pas d’accès à 10.42.0.1 — vérifie hyperdrive");
      } else {
        toast("Broker joignable, handshake MQTT encore en cours…");
      }
    } catch (err) {
      toast(err.message);
    }
  });

  $("btnDisconnect").addEventListener("click", async () => {
    try {
      const data = await api("/api/broker/disconnect", { method: "POST", body: "{}" });
      applySnapshot(data.state);
    } catch (err) {
      toast(err.message);
    }
  });

  $("btnDiscover").addEventListener("click", async () => {
    try {
      await api("/api/discover", { method: "POST", body: JSON.stringify({ enable: true }) });
      toast("Scan démarré");
    } catch (err) {
      toast(err.message);
    }
  });

  $("btnStopScan").addEventListener("click", async () => {
    try {
      await api("/api/discover", { method: "POST", body: JSON.stringify({ enable: false }) });
    } catch (err) {
      toast(err.message);
    }
  });

  $("btnAddManual").addEventListener("click", async () => {
    try {
      const id = $("manualId").value.trim();
      const data = await api("/api/vehicles/manual", {
        method: "POST",
        body: JSON.stringify({
          vehicleId: id,
          model: $("manualModel").value.trim() || null,
        }),
      });
      applySnapshot(data.state);
      if (id) await selectVehicleForControl(id);
      $("manualId").value = "";
    } catch (err) {
      toast(err.message);
    }
  });

  $("btnRaceStart").addEventListener("click", raceStart);
  $("btnRaceLap").addEventListener("click", raceLap);
  $("btnRaceStop").addEventListener("click", raceStop);
  $("btnRaceReset").addEventListener("click", raceReset);
  $("btnRacePanic").addEventListener("click", () => {
    panicAll().catch((err) => toast(err.message));
  });

  $("btnSaveProfile").addEventListener("click", () => {
    const name = $("profileName").value.trim();
    const slot = $("profileTarget").value;
    if (!name) {
      toast("Donne un nom au profil");
      return;
    }
    if (!state.slots[slot]) {
      toast(`Piste ${slot} vide`);
      return;
    }
    const settings = readCockpitSettings(slot);
    const list = loadCustomProfiles();
    list.push({
      id: `custom-${Date.now()}`,
      name,
      builtin: false,
      settings: {
        velocity: settings.velocity,
        acceleration: settings.acceleration,
        lane: settings.lane,
        lights: settings.lights,
      },
    });
    saveCustomProfiles(list);
    $("profileName").value = "";
    renderProfiles();
    toast(`Profil « ${name} » sauvé`);
  });

  $("btnClearLog").addEventListener("click", () => { logEl.innerHTML = ""; });

  $("btnDebugSnapshot").addEventListener("click", async () => {
    try {
      const data = await api("/api/debug/snapshot", { method: "POST", body: "{}" });
      dbg("INFO", "manual snapshot requested");
      toast("Snapshot écrit dans debug.log");
      logLine("in", `Snapshot réseau → ${data.logFile}`);
    } catch (err) {
      toast(err.message);
    }
  });

  $("btnDebugClear").addEventListener("click", async () => {
    try {
      await api("/api/debug/clear", { method: "POST", body: "{}" });
      toast("debug.log vidé");
    } catch (err) {
      toast(err.message);
    }
  });

  $("btnDebugOpenHint").addEventListener("click", async () => {
    try {
      const info = await api("/api/debug/info");
      $("debugPath").innerHTML = `Fichier : <code>${escapeHtml(info.logFile)}</code>`;
      toast("Chemin debug.log affiché");
      dbg("INFO", "user asked debug path", { logFile: info.logFile });
    } catch (err) {
      toast(err.message);
    }
  });

  $("btnHeroConnect").addEventListener("click", async () => {
    try {
      const id = state.selectedId || state.slots.A || requireSlot("A");
      await api(`/api/vehicles/${encodeURIComponent(id)}/connect`, {
        method: "POST",
        body: JSON.stringify({ enable: true }),
      });
      toast("Connect intent envoyé");
    } catch (err) {
      if (err.message !== "no vehicle") toast(err.message);
    }
  });

  $("btnHeroPanic").addEventListener("click", async () => {
    try {
      const id = state.selectedId || state.slots.A || requireSlot("A");
      const root = cockpitEl("A");
      root.querySelector(".speed-slider").value = 0;
      root.querySelector(".speed-value").textContent = "0";
      await api(`/api/vehicles/${encodeURIComponent(id)}/panic`, { method: "POST", body: "{}" });
      updateHero();
      toast("STOP");
    } catch (err) {
      if (err.message !== "no vehicle") toast(err.message);
    }
  });

  $("btnManualDrive").addEventListener("click", () => {
    setManualDrive(!state.manualDrive);
  });

  async function boot() {
    loadSlots();
    if (state.slots.A) state.selectedId = state.slots.A;
    try {
      state.meta = await api("/api/meta");
    } catch {
      state.meta = null;
    }
    wireCockpit("A");
    wireCockpit("B");
    renderProfiles();
    updateRaceUI();
    syncCockpitLayout();
    try {
      applySnapshot(await api("/api/state"));
    } catch {
      refreshCockpit("A");
      refreshCockpit("B");
    }
    syncCockpitLayout();
    connectWs();
    startStatePoll();
    try {
      const info = await api("/api/debug/info");
      if (info.logFile) {
        $("debugPath").innerHTML = `Fichier : <code>${escapeHtml(info.logFile)}</code>`;
      }
      dbg("INFO", "UI booted", { logFile: info.logFile });
    } catch {
      /* ignore */
    }
    await autoBoot();
  }

  boot();
})();
 