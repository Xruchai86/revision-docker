// Einstellungsseite: speichert bei jeder Aenderung sofort serverseitig
// (/config/settings.json), damit nichts durch vergessenes "Speichern" verloren
// geht. Der Statustext unten quittiert jede Speicherung kurz.

let rules = Array.isArray(window.SAVED_RULES) ? window.SAVED_RULES.slice() : [];

async function saveSettingsField(patch) {
  const status = document.getElementById("saveStatus");
  try {
    const res = await fetch("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    status.textContent = "Gespeichert.";
  } catch (err) {
    status.textContent = "Speichern fehlgeschlagen: " + err.message;
  }
  setTimeout(() => { status.textContent = ""; }, 2500);
}

// ---------------------------------------------------------------------------
// Ordner-Zuordnung
// ---------------------------------------------------------------------------
function renderRules() {
  const body = document.getElementById("rulesBody");
  body.innerHTML = "";
  if (rules.length === 0) {
    body.innerHTML = `<tr><td colspan="3" class="status-line">
      Noch keine Regeln – ohne Regeln bleibt die Kategorie auf der Hauptseite frei wählbar.
    </td></tr>`;
    return;
  }
  rules.forEach((rule, idx) => {
    const tr = document.createElement("tr");

    const tdPath = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "path-picker";

    // Pfad bleibt frei editierbar (Teilfragmente wie "Anime" sind erlaubt und
    // oft praktischer als ein voller Pfad), aber der Browser-Button verhindert
    // Tippfehler - eine Regel mit falschem Pfad greift sonst stillschweigend nie.
    const inpPath = document.createElement("input");
    inpPath.type = "text";
    inpPath.value = rule.pfad || "";
    inpPath.placeholder = "Ordner wählen oder Teilpfad eintippen";
    inpPath.addEventListener("change", e => {
      rules[idx].pfad = e.target.value;
      saveSettingsField({ category_rules: rules });
    });

    const btnBrowse = document.createElement("button");
    btnBrowse.className = "btn-ghost";
    btnBrowse.textContent = "Durchsuchen…";
    btnBrowse.onclick = () => openBrowser(idx);

    wrap.append(inpPath, btnBrowse);
    tdPath.appendChild(wrap);

    const tdCat = document.createElement("td");
    const sel = document.createElement("select");
    for (const [key, label] of Object.entries(window.CATEGORIES)) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = label;
      if (key === rule.kategorie) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener("change", e => {
      rules[idx].kategorie = e.target.value;
      saveSettingsField({ category_rules: rules });
    });
    tdCat.appendChild(sel);

    const tdDel = document.createElement("td");
    const acts = document.createElement("div");
    acts.className = "row-actions";

    // Reihenfolge ist relevant: die ERSTE passende Regel gewinnt. Ueberlappende
    // Fragmente (z.B. "06_Serien" und "06_Serien-Anime") wuerden sonst je nach
    // Reihenfolge stillschweigend falsch zuordnen - deshalb verschiebbar.
    const up = document.createElement("button");
    up.className = "btn-ghost";
    up.textContent = "↑";
    up.title = "Nach oben (wird früher geprüft)";
    up.disabled = idx === 0;
    up.onclick = () => {
      [rules[idx - 1], rules[idx]] = [rules[idx], rules[idx - 1]];
      renderRules();
      saveSettingsField({ category_rules: rules });
    };

    const down = document.createElement("button");
    down.className = "btn-ghost";
    down.textContent = "↓";
    down.title = "Nach unten (wird später geprüft)";
    down.disabled = idx === rules.length - 1;
    down.onclick = () => {
      [rules[idx + 1], rules[idx]] = [rules[idx], rules[idx + 1]];
      renderRules();
      saveSettingsField({ category_rules: rules });
    };

    const btn = document.createElement("button");
    btn.className = "btn-ghost";
    btn.textContent = "✕";
    btn.title = "Regel entfernen";
    btn.onclick = () => {
      rules.splice(idx, 1);
      renderRules();
      saveSettingsField({ category_rules: rules });
    };

    acts.append(up, down, btn);
    tdDel.appendChild(acts);

    tr.append(tdPath, tdCat, tdDel);
    body.appendChild(tr);
  });
}

function addRule() {
  const firstCat = Object.keys(window.CATEGORIES)[0];
  rules.push({ pfad: "", kategorie: firstCat });
  renderRules();
}

// ---------------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", () => {
  renderRules();

  document.getElementById("outputFolder").addEventListener("change", e =>
    saveSettingsField({ output_folder: e.target.value }));

  document.querySelectorAll(".cat-output").forEach(inp =>
    inp.addEventListener("change", saveOutputFolders));

  const q = document.getElementById("qualityOverride");
  const qVal = document.getElementById("qualityValue");
  q.addEventListener("input", e => {
    qVal.textContent = e.target.value === "0" ? "Profil-Standard" : e.target.value;
  });
  q.addEventListener("change", e =>
    saveSettingsField({ quality_override: parseInt(e.target.value, 10) }));
  if (q.value === "0") qVal.textContent = "Profil-Standard";

  document.getElementById("downsizeThreshold").addEventListener("change", e =>
    saveSettingsField({ downsize_threshold_mbps: parseFloat(e.target.value) || 35.0 }));

  document.getElementById("forceReencodeCheck").addEventListener("change", e =>
    saveSettingsField({ force_reencode_dual_layer: e.target.checked }));
});


// ---------------------------------------------------------------------------
// Ordner-Browser (derselbe /api/browse-Endpunkt wie auf der Hauptseite).
// Uebernommen wird der Pfad RELATIV zum Medien-Root - genau das, was die
// Regelpruefung serverseitig als Fragment im Quellpfad sucht.
// ---------------------------------------------------------------------------
let browseCurrentPath = "";
let browseCurrentRoot = "";   // absoluter Pfad der aktiven Wurzel
let browseTargetIdx = null;   // Index einer Zuordnungsregel (Quellbaum)
let browseOutputTarget;       // undefined = nicht aktiv, null = allgemeiner
                              // Zielordner, sonst Kategorie-Schluessel
let browseRootKey = "source";

async function openBrowser(idx) {
  browseTargetIdx = idx;
  browseOutputTarget = undefined;
  browseRootKey = "source";
  document.getElementById("browseTitle").textContent = "Ordner für diese Regel wählen";
  document.getElementById("browseModal").style.display = "flex";
  await browseTo("");
}

// Zielordner-Browser: laeuft gegen die Ausgabe-Wurzel statt gegen den Quellbaum.
// category = null bedeutet "allgemeiner Zielordner".
async function openOutputBrowser(category) {
  browseTargetIdx = null;
  browseOutputTarget = category;
  browseRootKey = "output";
  document.getElementById("browseTitle").textContent =
    category === null ? "Allgemeinen Zielordner wählen"
                      : `Zielordner für ${window.CATEGORIES[category]} wählen`;
  document.getElementById("browseModal").style.display = "flex";
  await browseTo("");
}

function closeBrowser() {
  document.getElementById("browseModal").style.display = "none";
  browseTargetIdx = null;
  browseOutputTarget = undefined;
}

async function browseTo(relPath) {
  const res = await fetch(
    `/api/browse?root=${encodeURIComponent(browseRootKey)}&path=${encodeURIComponent(relPath)}`);
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  browseCurrentPath = data.rel_path;
  browseCurrentRoot = data.root;

  const crumbEl = document.getElementById("browseBreadcrumb");
  const segments = data.rel_path ? data.rel_path.split("/") : [];
  let html = `<span class="crumb" onclick="browseTo('')">📁 ${data.root}</span>`;
  let acc = "";
  for (const seg of segments) {
    acc = acc ? `${acc}/${seg}` : seg;
    html += ` / <span class="crumb" onclick="browseTo('${acc.replace(/'/g, "\\'")}')">${seg}</span>`;
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
    const child = data.rel_path ? `${data.rel_path}/${folder}` : folder;
    div.onclick = () => browseTo(child);
    listEl.appendChild(div);
  }
}

function chooseCurrentFolder() {
  // Zielordner: absoluter Pfad, denn dorthin wird tatsaechlich geschrieben.
  if (browseOutputTarget !== undefined) {
    const abs = browseCurrentPath ? `${browseCurrentRoot}/${browseCurrentPath}` : browseCurrentRoot;
    if (browseOutputTarget === null) {
      document.getElementById("outputFolder").value = abs;
      saveSettingsField({ output_folder: abs });
    } else {
      const inp = document.querySelector(`.cat-output[data-category="${browseOutputTarget}"]`);
      if (inp) inp.value = abs;
      saveOutputFolders();
    }
    closeBrowser();
    return;
  }

  // Zuordnungsregel: relativer Pfad, denn danach wird im Quellpfad gesucht.
  if (browseTargetIdx === null) return;
  if (!browseCurrentPath) {
    alert("Bitte einen Unterordner wählen – der Medien-Root selbst würde auf alles passen.");
    return;
  }
  rules[browseTargetIdx].pfad = browseCurrentPath;
  closeBrowser();
  renderRules();
  saveSettingsField({ category_rules: rules });
}

function saveOutputFolders() {
  const map = {};
  document.querySelectorAll(".cat-output").forEach(inp => {
    const v = inp.value.trim();
    if (v) map[inp.dataset.category] = v;
  });
  saveSettingsField({ output_folders: map });
}

// Gemessenen Kategorie-Wert verwerfen. Danach greift wieder der globale Regler
// bzw. der Preset-Standard - es bleibt also nie ohne Wert.
async function clearCategoryQuality(category) {
  const map = Object.assign({}, window.QUALITY_BY_CATEGORY || {});
  delete map[category];
  window.QUALITY_BY_CATEGORY = map;
  await saveSettingsField({ quality_by_category: map });
  location.reload();
}
