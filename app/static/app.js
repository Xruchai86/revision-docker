let lastResults = [];
const ACTION_LABELS = {
  dual_layer: "Dual-Layer-Fix (verlustfrei)",
  relabel: "Relabel → 8.1 (verlustfrei)",
  reencode: "Reencode-Fix (QSV)",
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
  document.getElementById("profileSelect").addEventListener("change", e =>
    saveSettingsField({ quality_profile: e.target.value }));
  document.getElementById("downsizeThreshold").addEventListener("change", e =>
    saveSettingsField({ downsize_threshold_mbps: parseFloat(e.target.value) || 35.0 }));
});

async function scan() {
  const folder = document.getElementById("inputFolder").value.trim();
  const statusEl = document.getElementById("scanStatus");
  if (!folder) { statusEl.textContent = "Bitte zuerst einen Quellordner angeben."; return; }

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
      <td class="status-cell status-queued">–</td>
    `;
    body.appendChild(tr);
  }
  document.getElementById("resultsTable").style.display = data.results.length ? "table" : "none";
  document.getElementById("actionsRow").style.display = data.results.length ? "block" : "none";
}

function toggleAll(cb) {
  document.querySelectorAll(".rowcheck").forEach(el => el.checked = cb.checked);
}

async function processSelected() {
  const checked = Array.from(document.querySelectorAll(".rowcheck:checked"));
  const outputFolder = document.getElementById("outputFolder").value.trim();
  const profile = document.getElementById("profileSelect").value;

  if (checked.length === 0) { alert("Keine Datei angehakt."); return; }
  if (!outputFolder) { alert("Bitte einen Zielordner angeben."); return; }

  const fixPaths = checked.filter(el => el.dataset.mode === "fix").map(el => el.dataset.path);
  const downsizePaths = checked.filter(el => el.dataset.mode === "downsize").map(el => el.dataset.path);

  if (fixPaths.length) {
    await fetch("/api/fix", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: fixPaths, output_folder: outputFolder, profile }),
    });
  }
  if (downsizePaths.length) {
    await fetch("/api/downsize", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: downsizePaths, output_folder: outputFolder, profile }),
    });
  }
}

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
