let lastResults = [];
let browseCurrentPath = "";
const ACTION_LABELS = {
  dual_layer: "Dual-Layer-Fix (verlustfrei)",
  relabel: "Relabel → 8.1 (verlustfrei)",
  reencode: "Reencode-Fix (VAAPI)",
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
  document.getElementById("outputFolder").addEventListener("change", e =>
    saveSettingsField({ output_folder: e.target.value }));
  document.getElementById("profileSelect").addEventListener("change", e => {
    saveSettingsField({ quality_profile: e.target.value });
    // Profilwechsel setzt den Regler auf den Profil-Standardwert zurueck -
    // der Nutzer kann danach weiterhin selbst nachjustieren.
    const defaultMbps = window.PROFILE_TARGET_MBPS[e.target.value];
    if (defaultMbps) {
      document.getElementById("targetBitrateSlider").value = defaultMbps;
      document.getElementById("targetBitrateValue").textContent = defaultMbps;
      saveSettingsField({ target_bitrate_mbps: defaultMbps });
    }
  });
  document.getElementById("targetBitrateSlider").addEventListener("change", e =>
    saveSettingsField({ target_bitrate_mbps: parseFloat(e.target.value) }));
  document.getElementById("downsizeThreshold").addEventListener("change", e =>
    saveSettingsField({ downsize_threshold_mbps: parseFloat(e.target.value) || 35.0 }));
  document.getElementById("forceReencodeCheck").addEventListener("change", e =>
    saveSettingsField({ force_reencode_dual_layer: e.target.checked }));
});

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
  await saveSettingsField({ downsize_threshold_mbps: parseFloat(document.getElementById("downsizeThreshold").value) || 35.0 });

  statusEl.textContent = "Scanne…";
  const res = await fetch("/api/scan", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folder }),
  });
  const data = await res.json();
  if (data.error) { statusEl.textContent = data.error; return; }

  lastResults = data.results;
  statusEl.textContent = `${data.results.length} Datei(en) gefunden (Downsize-Schwelle: ${data.downsize_threshold_mbps} Mbit/s).`;

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
    tr.innerHTML = `
      <td><input type="checkbox" class="rowcheck" data-path="${r.path}" data-mode="${isFix ? "fix" : "downsize"}"></td>
      <td>${r.filename}<br><span style="color:var(--muted);font-size:11px">${r.container} · ${r.resolution} · ${r.bitrate_mbps} Mbit/s · DV ${r.dv_profile ?? "-"}</span></td>
      <td>${tagHtml}</td>
    `;
    body.appendChild(tr);
  }
  document.getElementById("selectAll").checked = false;
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
  const outputFolder = document.getElementById("outputFolder").value.trim();
  const profile = document.getElementById("profileSelect").value;
  const target_bitrate_mbps = parseFloat(document.getElementById("targetBitrateSlider").value);

  if (checked.length === 0) { alert("Keine Datei angehakt."); return; }
  if (!outputFolder) { alert("Bitte einen Zielordner angeben."); return; }

  const fixPaths = checked.filter(el => el.dataset.mode === "fix").map(el => el.dataset.path);
  const downsizePaths = checked.filter(el => el.dataset.mode === "downsize").map(el => el.dataset.path);

  if (fixPaths.length) {
    await fetch("/api/fix", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: fixPaths, output_folder: outputFolder, profile, target_bitrate_mbps }),
    });
  }
  if (downsizePaths.length) {
    await fetch("/api/downsize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: downsizePaths, output_folder: outputFolder, profile, target_bitrate_mbps }),
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
      <td>${job.job_type === "downsize" ? "Downsize" : "Fix"}</td>
      <td class="status-${job.status}">${job.status}${job.error ? " – " + job.error : ""}</td>
      <td><button class="btn-ghost" onclick="showLog('${job.id}')">Log</button></td>
    `;
    body.appendChild(tr);
  }
  setTimeout(pollJobs, 2000);
}

async function showLog(jobId) {
  const res = await fetch(`/api/jobs/${jobId}/log`);
  const data = await res.json();
  document.getElementById("logContent").textContent = data.log || "(noch kein Log)";
  document.getElementById("logModal").style.display = "flex";
}

function closeLog() {
  document.getElementById("logModal").style.display = "none";
}

pollJobs();
