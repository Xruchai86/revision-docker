let lastResults = [];
// Job-Arten fuer die Warteschlange. Frueher stand hier "downsize ? Downsize : Fix" -
// dadurch erschien auch die Kalibrierung als "Fix".
const JOB_TYPE_LABELS = { fix: "Fix", downsize: "Downsize", calibrate: "Kalibrierung" };
let browseCurrentPath = "";
const ACTION_LABELS = {
  dual_layer: "Dual-Layer-Fix (verlustfrei)",
  relabel: "Relabel → 8.1 (verlustfrei)",
  reencode: "Reencode-Fix",  // Encoder haengt am gewaehlten Preset, nicht fest verdrahtet
};

// Persistiert Zielordner/Profil/Schwelle serverseitig (settings.json), sobald
// sie sich aendern - damit sie nach einem Container-Neustart erhalten bleiben.
async function saveSettingsField(patch) {
  await fetch("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

document.addEventListener("DOMContentLoaded", () => {
  // Zielordner, Downsize-Schwelle, Qualitaetswert und die Reencode-Option leben
  // jetzt auf /einstellungen - hier bleiben nur die Felder, die sich pro
  // Aufgabe aendern.
  applyCategoryFilter(window.SAVED_CATEGORY || "");

  document.getElementById("categorySelect").addEventListener("change", e =>
    applyCategoryFilter(e.target.value));

  document.getElementById("profileSelect").addEventListener("change", e => {
    saveSettingsField({ quality_profile: e.target.value });
    const defaultMbps = window.PROFILE_TARGET_MBPS[e.target.value];
    if (defaultMbps) {
      const slider = document.getElementById("targetBitrateSlider");
      slider.value = defaultMbps;
      document.getElementById("targetBitrateValue").textContent = defaultMbps;
      saveSettingsField({ target_bitrate_mbps: defaultMbps });
    }
  });

  document.getElementById("targetBitrateSlider").addEventListener("change", e =>
    saveSettingsField({ target_bitrate_mbps: parseFloat(e.target.value) }));
});

// Zeigt nur die Profile der gewaehlten Kategorie. Leere Auswahl = alle.
// Faellt das aktuell gewaehlte Profil aus der Liste, wird automatisch das
// erste passende genommen, damit nie ein unsichtbares Profil aktiv bleibt.
function applyCategoryFilter(category) {
  const sel = document.getElementById("profileSelect");
  const catSel = document.getElementById("categorySelect");
  if (catSel.value !== category) catSel.value = category;

  let firstVisible = null;
  let currentStillVisible = false;
  for (const opt of sel.options) {
    const cats = window.PROFILE_CATEGORIES[opt.value] || [];
    const visible = !category || cats.includes(category);
    opt.hidden = !visible;
    if (visible) {
      if (!firstVisible) firstVisible = opt.value;
      if (opt.value === sel.value) currentStillVisible = true;
    }
  }
  if (!currentStillVisible && firstVisible) {
    sel.value = firstVisible;
    saveSettingsField({ quality_profile: firstVisible });
    const defaultMbps = window.PROFILE_TARGET_MBPS[firstVisible];
    if (defaultMbps) {
      document.getElementById("targetBitrateSlider").value = defaultMbps;
      document.getElementById("targetBitrateValue").textContent = defaultMbps;
    }
  }
}

// ---------------------------------------------------------------------------
// Ordner-Browser-Popup - navigiert innerhalb des gemounteten Medien-Roots,
// keine manuelle Pfadeingabe mehr noetig.
// ---------------------------------------------------------------------------
async function openBrowser() {
  document.getElementById("browseModal").style.display = "flex";
  await browseTo("");
}

function closeBrowser() {
  document.getElementById("browseModal").style.display = "none";
}

async function browseTo(relPath) {
  const res = await fetch(`/api/browse?path=${encodeURIComponent(relPath)}`);
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  browseCurrentPath = data.rel_path;

  // Breadcrumb aus dem relativen Pfad aufbauen - jedes Segment einzeln anklickbar.
  const crumbEl = document.getElementById("browseBreadcrumb");
  const segments = data.rel_path ? data.rel_path.split("/") : [];
  let html = `<span class="crumb" onclick="browseTo('')">📁 ${data.root}</span>`;
  let accPath = "";
  for (const seg of segments) {
    accPath = accPath ? `${accPath}/${seg}` : seg;
    const target = accPath;
    html += ` / <span class="crumb" onclick="browseTo('${target.replace(/'/g, "\\'")}')">${seg}</span>`;
  }
  crumbEl.innerHTML = html;

  const listEl = document.getElementById("browseList");
  listEl.innerHTML = "";
  if (data.folders.length === 0) {
    listEl.innerHTML = `<div class="status-line">Keine Unterordner hier.</div>`;
  }
  for (const folder of data.folders) {
    const div = document.createElement("div");
    div.className = "browse-item";
    div.textContent = "📁 " + folder;
    const childPath = data.rel_path ? `${data.rel_path}/${folder}` : folder;
    div.onclick = () => browseTo(childPath);
    listEl.appendChild(div);
  }
}

function chooseCurrentFolder() {
  document.getElementById("inputFolder").value = "/media/source" +
    (browseCurrentPath ? "/" + browseCurrentPath : "");
  document.getElementById("scanBtn").disabled = false;
  closeBrowser();
  scan();
}

// ---------------------------------------------------------------------------
// Scan + Ergebnisse-Popup
// ---------------------------------------------------------------------------
async function scan() {
  const folder = document.getElementById("inputFolder").value.trim();
  const statusEl = document.getElementById("scanStatus");
  if (!folder) { statusEl.textContent = "Bitte zuerst einen Quellordner wählen."; return; }

  // Aktuelle Schwelle vor dem Scan speichern, damit der Server sie fuer die
  // can_downsize-Einordnung verwendet.

  statusEl.textContent = "Scanne…";
  const res = await fetch("/api/scan", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folder }),
  });
  const data = await res.json();
  if (data.error) { statusEl.textContent = data.error; return; }

  lastResults = data.results;

  // Kategorie aus den Ordnerregeln uebernehmen, falls der Pfad zu einer passt.
  let catNote = "";
  if (data.category && window.PROFILE_CATEGORIES) {
    applyCategoryFilter(data.category);
    const label = document.getElementById("categorySelect").selectedOptions[0];
    catNote = ` · Kategorie automatisch: ${label ? label.textContent : data.category}`;
  }

  statusEl.textContent =
    `${data.results.length} Datei(en) gefunden (Downsize-Schwelle: ${data.downsize_threshold_mbps} Mbit/s)${catNote}.`;

  if (data.results.length === 0) return;

  document.getElementById("resultsCount").textContent = `${data.results.length} Datei(en)`;
  const body = document.getElementById("resultsBody");
  body.innerHTML = "";
  for (const r of data.results) {
    const isFix = r.action !== "none" && r.action !== "unsupported";
    const tagHtml = isFix
      ? `<span class="tag ${r.action}">${ACTION_LABELS[r.action] ?? r.action}</span>`
      : `<span class="tag downsize">Downsize</span>`;
    const tr = document.createElement("tr");
    const dyn = r.is_sdr ? "SDR" : (r.dv_profile ? `DV ${r.dv_profile}` : "HDR10");
    tr.innerHTML = `
      <td><input type="checkbox" class="rowcheck" data-path="${r.path}" data-mode="${isFix ? "fix" : "downsize"}"></td>
      <td>${r.filename}<br><span style="color:var(--muted);font-size:11px">${r.container} · ${r.resolution} · ${r.bitrate_mbps} Mbit/s · ${dyn}</span></td>
      <td></td>
      <td>${tagHtml}</td>
    `;
    // Profil je Datei: SDR und HDR liegen oft im selben Ordner, deshalb muss
    // die Wahl pro Zeile moeglich sein. Vorbelegt wird passend zum Material.
    const sel = buildProfileSelect(r.is_sdr);
    sel.className = "rowprofile";
    sel.dataset.path = r.path;
    tr.children[2].appendChild(sel);
    body.appendChild(tr);
  }
  document.getElementById("selectAll").checked = false;
  fillBulkProfile();
  document.getElementById("resultsModal").style.display = "flex";
}

function closeResults() {
  document.getElementById("resultsModal").style.display = "none";
}

function toggleAll(cb) {
  document.querySelectorAll(".rowcheck").forEach(el => el.checked = cb.checked);
}

async function processSelected() {
  const checked = Array.from(document.querySelectorAll(".rowcheck:checked"));
  // Zielordner liegt jetzt in den Einstellungen, nicht mehr im Formular.
  let outputFolder = "";
  try {
    const cfg = await (await fetch("/api/settings")).json();
    outputFolder = (cfg.output_folder || "").trim();
  } catch (err) {
    alert("Einstellungen nicht lesbar: " + err.message);
    return;
  }
  const profile = document.getElementById("profileSelect").value;
  const target_bitrate_mbps = parseFloat(document.getElementById("targetBitrateSlider").value);
  // Gewaehlte Kategorie mitsenden: sie bestimmt serverseitig Qualitaetswert und
  // Zielordner. Ohne das wuerde eine manuelle Umstellung nur die Profilliste
  // filtern - wichtig z.B. bei Animationsfilmen, die im selben Ordner wie
  // Realfilme liegen und die keine Pfadregel unterscheiden kann.
  const category = document.getElementById("categorySelect").value || null;

  if (checked.length === 0) { alert("Keine Datei angehakt."); return; }
  if (!outputFolder) {
    alert("Kein Zielordner gesetzt – bitte unter „Einstellungen“ eintragen.");
    return;
  }

  const fixPaths = checked.filter(el => el.dataset.mode === "fix").map(el => el.dataset.path);
  const downsizePaths = checked.filter(el => el.dataset.mode === "downsize").map(el => el.dataset.path);

  // Profil je Datei einsammeln - ohne das würde für alle dasselbe gelten.
  const profile_map = {};
  for (const cb of checked) {
    const sel = document.querySelector(`.rowprofile[data-path="${CSS.escape(cb.dataset.path)}"]`);
    if (sel) profile_map[cb.dataset.path] = sel.value;
  }

  if (fixPaths.length) {
    await fetch("/api/fix", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: fixPaths, output_folder: outputFolder, profile, target_bitrate_mbps, category, profile_map }),
    });
  }
  if (downsizePaths.length) {
    await fetch("/api/downsize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: downsizePaths, output_folder: outputFolder, profile, target_bitrate_mbps, category, profile_map }),
    });
  }
  closeResults();
}

// ---------------------------------------------------------------------------
// Warteschlange
// ---------------------------------------------------------------------------
async function pollJobs() {
  const res = await fetch("/api/jobs");
  const data = await res.json();
  const body = document.getElementById("jobsBody");
  body.innerHTML = "";
  for (const job of data.jobs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${job.filename}</td>
      <td>${JOB_TYPE_LABELS[job.job_type] || job.job_type}</td>
      <td class="status-${job.status}">${job.status}${job.error ? " – " + job.error : ""}</td>
      <td>
        <button class="btn-ghost" onclick="showLog('${job.id}')">Log</button>
        ${job.job_type === "calibrate" && job.status === "done"
          ? `<button class="btn-gold" onclick="openCalibrationResult('${job.id}')">Ergebnis</button>` : ""}
      </td>
    `;
    body.appendChild(tr);
  }
  setTimeout(pollJobs, 2000);
}

// Live-Log: solange der Dialog offen ist, wird der Inhalt regelmaessig
// nachgeladen. Frueher zeigte der Dialog nur den Stand zum Zeitpunkt des
// Oeffnens - bei laufenden Jobs also einen eingefrorenen Ausschnitt.
let logTimer = null;
let logJobId = null;

async function refreshLog() {
  if (!logJobId) return;
  try {
    const res = await fetch(`/api/jobs/${logJobId}/log`);
    const data = await res.json();
    const el = document.getElementById("logContent");

    // Nur ans Ende springen, wenn der Nutzer ohnehin schon unten war -
    // sonst reisst es ihn beim Lesen staendig nach unten.
    const box = el.parentElement;
    const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;

    el.textContent = data.log || "(noch kein Log)";
    if (wasAtBottom) box.scrollTop = box.scrollHeight;
  } catch (err) {
    // Netzwerkaussetzer nicht als Fehler anzeigen - naechster Durchlauf holt es nach.
  }
}

async function showLog(jobId) {
  logJobId = jobId;
  document.getElementById("logModal").style.display = "flex";
  await refreshLog();
  document.getElementById("logContent").parentElement.scrollTop = 1e9;
  if (logTimer) clearInterval(logTimer);
  logTimer = setInterval(refreshLog, 2000);
}

function closeLog() {
  document.getElementById("logModal").style.display = "none";
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
  logJobId = null;
}

pollJobs();

// ---------------------------------------------------------------------------
// Qualitäts-Kalibrierung (VMAF)
// Der Knopf erscheint nur, wenn das Image ein VMAF-fähiges ffmpeg enthält -
// sonst würde er an einer fehlenden Bibliothek scheitern.
// ---------------------------------------------------------------------------
let calTimer = null;
let calJobId = null;

async function checkVmafAvailable() {
  try {
    const res = await fetch("/api/vmaf/status");
    const data = await res.json();
    if (data.available) document.getElementById("calibrateBtn").style.display = "";
  } catch (err) { /* ohne Status bleibt der Knopf verborgen */ }
}

async function startCalibration() {
  const checked = Array.from(document.querySelectorAll(".rowcheck:checked"));
  if (checked.length !== 1) {
    alert("Bitte genau eine Datei anhaken – kalibriert wird an einem Beispiel.");
    return;
  }
  const profile = document.getElementById("profileSelect").value;
  const res = await fetch("/api/calibrate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: checked[0].dataset.path, profile }),
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  calJobId = data.job_id;
  closeResults();
  document.getElementById("calTable").style.display = "none";
  document.getElementById("calStatus").textContent =
    `Messung läuft für die Werte ${data.quality_values.join(", ")} – das dauert einige Minuten. ` +
    `Fortschritt steht im Log des Jobs „${checked[0].dataset.path.split("/").pop()}“.`;
  document.getElementById("calModal").style.display = "flex";
  if (calTimer) clearInterval(calTimer);
  calTimer = setInterval(pollCalibration, 3000);
}

async function pollCalibration() {
  if (!calJobId) return;
  const res = await fetch("/api/jobs");
  const data = await res.json();
  const job = data.jobs.find(j => j.id === calJobId);
  if (!job) return;

  if (job.status === "failed") {
    document.getElementById("calStatus").textContent = "Fehlgeschlagen: " + (job.error || "");
    clearInterval(calTimer); calTimer = null;
    return;
  }
  if (job.status !== "done" || !job.calibration) return;

  clearInterval(calTimer); calTimer = null;
  document.getElementById("calStatus").textContent = "Fertig – Ergebnis:";

  const body = document.getElementById("calBody");
  body.innerHTML = "";
  // Empfehlung: niedrigster Wert (= kleinste Datei), der noch >= 93 erreicht.
  const inRange = job.calibration.filter(r => r.vmaf >= 93);
  const best = inRange.length ? Math.max(...inRange.map(r => r.quality)) : null;

  const catSel = document.getElementById("categorySelect");
  const cat = catSel.value;
  const catLabel = cat ? catSel.selectedOptions[0].textContent : null;

  for (const r of job.calibration) {
    const tr = document.createElement("tr");
    // Welche Stufe dieser Messwert bedient (falls überhaupt eine)
    const tierName = Object.entries(job.tiers || {})
      .find(([, e]) => e.quality === r.quality)?.[0];
    const mark = tierName ? `<span class="tag relabel">${tierName}</span>` : "";
    const p5 = (r.vmaf_p5 ?? "–");
    const band = (r.cambi === null || r.cambi === undefined) ? "–"
      : `${r.cambi}${r.cambi_max ? ` <span class="status-line">(Spitze ${r.cambi_max})</span>` : ""}`;
    tr.innerHTML = `<td>${r.quality}</td><td>${r.vmaf}</td><td>${p5}</td><td>${band}</td>` +
      `<td>${r.bitrate_mbps} Mbit/s</td><td>${r.estimated_gb} GB</td><td>${mark}</td>`;
    body.appendChild(tr);
  }

  const tierCount = Object.keys(job.tiers || {}).length;
  const st = document.getElementById("calStatus");
  if (job.calibration_warning) {
    // Unplausible Messung: Werte zeigen (zur Diagnose), aber nichts anbieten.
    st.innerHTML = `<strong style="color:var(--red,#e05555)">⚠ ${job.calibration_warning}</strong>`;
  } else if (!cat) {
    st.textContent = "Fertig. Zum Übernehmen oben eine Kategorie wählen und dieses Ergebnis " +
      "über „Ergebnis“ in der Warteschlange erneut öffnen – neu messen ist nicht nötig.";
  } else if (tierCount === 0) {
    st.textContent = "Fertig – aber kein gemessener Wert erreicht die sparsame Stufe " +
      "(Ø 90 bei höchstens 6 Punkten Einbruch). Bitte mit niedrigeren Qualitätswerten erneut messen.";
  } else {
    st.innerHTML = `Fertig – ${tierCount} von 3 Stufen belegt. ` +
      `Übernehmen setzt sie gemeinsam für „${catLabel}“.`;
    const btn = document.createElement("button");
    btn.className = "btn-gold";
    btn.style.marginTop = "10px";
    btn.textContent = `Alle Stufen für ${catLabel} übernehmen`;
    btn.onclick = () => applyCalibration(cat, catLabel, job.tiers, job.filename);
    st.appendChild(document.createElement("br"));
    st.appendChild(btn);
  }
  document.getElementById("calTable").style.display = "";
}

function closeCal() {
  document.getElementById("calModal").style.display = "none";
  if (calTimer) { clearInterval(calTimer); calTimer = null; }
  calJobId = null;
}

checkVmafAvailable();


// Übernimmt einen gemessenen Wert dauerhaft für eine Kategorie.
async function applyCalibration(category, categoryLabel, tiers, sourceFile) {
  try {
    const res = await fetch("/api/calibrate/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category, tiers, source: sourceFile || "" }),
    });
    const data = await res.json();
    if (data.error) { alert(data.error); return; }
    const list = Object.entries(tiers)
      .map(([t, e]) => `${t}: Qualität ${e.quality} (Ø ${e.vmaf}` +
        (e.vmaf_p5 != null ? `, schwächste 5 % ${e.vmaf_p5}` : "") + ")").join(", ");
    document.getElementById("calStatus").textContent =
      `Gespeichert für „${categoryLabel}“ – ${list}. Gilt ab sofort für alle Dateien dieser Kategorie.`;
  } catch (err) {
    alert("Speichern fehlgeschlagen: " + err.message);
  }
}


// ---------------------------------------------------------------------------
// Profil-Auswahl je Datei und als Sammelaktion
// ---------------------------------------------------------------------------

// Baut ein Dropdown mit den Profilen der aktuellen Kategorie. Vorbelegt wird
// nach Material: SDR-Dateien bekommen ein SDR-Profil, HDR-Dateien ein HDR-Profil.
function buildProfileSelect(isSdr) {
  const category = document.getElementById("categorySelect").value;
  const sel = document.createElement("select");
  let preferred = null;

  for (const [key, name] of Object.entries(window.PROFILE_NAMES || {})) {
    const cats = window.PROFILE_CATEGORIES[key] || [];
    if (category && !cats.includes(category)) continue;
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = name;
    sel.appendChild(opt);

    // Vorauswahl: empfohlene Stufe, passend zu SDR bzw. HDR
    const sdrProfile = window.PROFILE_SDR[key] === true;
    if (!preferred && window.PROFILE_TIERS[key] === "empfohlen" && sdrProfile === !!isSdr) {
      preferred = key;
    }
  }
  if (preferred) sel.value = preferred;
  return sel;
}

// Setzt das oben gewählte Profil auf alle angehakten Zeilen.
function applyBulkProfile() {
  const target = document.getElementById("bulkProfile").value;
  if (!target) return;
  const checked = Array.from(document.querySelectorAll(".rowcheck:checked"));
  if (checked.length === 0) { alert("Keine Datei angehakt."); return; }
  let changed = 0;
  for (const cb of checked) {
    const sel = document.querySelector(`.rowprofile[data-path="${CSS.escape(cb.dataset.path)}"]`);
    if (sel && Array.from(sel.options).some(o => o.value === target)) {
      sel.value = target; changed++;
    }
  }
  const note = changed === checked.length
    ? `Profil für ${changed} Datei(en) gesetzt.`
    : `Profil für ${changed} von ${checked.length} Datei(en) gesetzt – der Rest bietet es nicht an.`;
  document.getElementById("resultsCount").textContent = note;
}

// Füllt die Sammelauswahl mit denselben Profilen wie die Zeilen.
function fillBulkProfile() {
  const bulk = document.getElementById("bulkProfile");
  if (!bulk) return;
  const fresh = buildProfileSelect(false);
  bulk.innerHTML = fresh.innerHTML;
  bulk.value = fresh.value;
}


// Öffnet das Ergebnis einer abgeschlossenen Kalibrierung aus der Warteschlange.
// Vorher war das Ergebnis nur sichtbar, solange der Dialog ab dem Start offen
// blieb - einmal geschlossen oder Seite neu geladen, war es nicht mehr erreichbar.
function openCalibrationResult(jobId) {
  calJobId = jobId;
  document.getElementById("calTable").style.display = "none";
  document.getElementById("calStatus").textContent = "Lade Ergebnis…";
  document.getElementById("calModal").style.display = "flex";
  pollCalibration();
}
