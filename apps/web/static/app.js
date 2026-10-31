const form = document.querySelector("#race-form");
const analyzeButton = form.querySelector(".analyze-button");
const raceDate = document.querySelector("#race-date");
const racecourse = document.querySelector("#racecourse");
const raceNumber = document.querySelector("#race-number");
const progress = document.querySelector("#progress");
const errorBox = document.querySelector("#error");
const tabs = document.querySelector("#tabs");
const summary = document.querySelector("#summary");
const openHistoryButton = document.querySelector("#open-history");
const exportDataButton = document.querySelector("#export-data");
const importDataButton = document.querySelector("#import-data");
const diagnoseProviderButton = document.querySelector("#diagnose-provider");
const localAdminTools = document.querySelector("#local-admin-tools");
const importFile = document.querySelector("#import-file");
let current = null;
function materialIcon(name) {
  const key = name === "calendar-alt" ? "calendar" : name;
  const paths = {
    clipboard: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4.5h6M8 10h8M8 14h8M8 18h5"/>',
    lightbulb: '<path d="M9 18h6M10 21h4M8.2 14.5a6 6 0 1 1 7.6 0c-.9.7-1.3 1.5-1.4 2.5h-4.8c-.1-1-.5-1.8-1.4-2.5Z"/>',
    star: '<path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z"/>',
    sparkles: '<path d="m12 3 1.5 5.5L19 10l-5.5 1.5L12 17l-1.5-5.5L5 10l5.5-1.5L12 3ZM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15ZM5 15l.6 1.4L7 17l-1.4.6L5 19l-.6-1.4L3 17l1.4-.6L5 15Z"/>',
    download: '<path d="M12 3v12m0 0 4-4m-4 4-4-4M4 19v2h16v-2"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.5 9a7 7 0 0 1 12-2l2.5 3M4 14l2.5 3a7 7 0 0 0 12-2"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    search: '<circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/>',
    trophy: '<path d="M8 4h8v4a4 4 0 0 1-8 0V4ZM8 6H4v2a4 4 0 0 0 4 4m8-6h4v2a4 4 0 0 1-4 4M12 12v5m-4 3h8m-7 0v-3h6v3"/>',
    "chart-line": '<path d="M4 19V5m0 14h17M7 15l4-4 3 2 5-6"/>',
    analytics: '<path d="M4 19V5m0 14h17M7 15l3-4 3 2 5-6"/><circle cx="18" cy="7" r="1"/>'
  };
  const svg = paths[key] || paths.sparkles;
  return `<svg class="app-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${svg}</svg>`;
}
const LAST_ANALYSIS_KEY = "horseprediction:last-analysis";
const UI_STATE_KEY = "horseprediction:ui-state";
const RESULT_REFRESH_INTERVAL_MS = 15000;
const SCHEDULE_CACHE_TTL_MS = 5 * 60 * 1000;
let availableSchedule = null;
let scheduleRequestId = 0;
const scheduleCache = new Map();
const historyFilters = {
  racecourse: "",
  hasResult: "",
  hasReview: ""
};

const today = new Date();
const todayValue = [
  today.getFullYear(),
  String(today.getMonth() + 1).padStart(2, "0"),
  String(today.getDate()).padStart(2, "0")
].join("-");
raceDate.value = todayValue;

loadDeploymentConfig();

async function loadDeploymentConfig() {
  try {
    const response = await fetch("/api/config");
    if (!response.ok) return;
    const config = await response.json();
    if (config.deployment_mode === "local") {
      localAdminTools.classList.remove("hidden");
      return;
    }
    analyzeButton.innerHTML = `${materialIcon("clipboard")}<span>保存済み分析を表示</span>`;
  } catch {
    // Keep administration controls hidden unless the server confirms local mode.
  }
}

function clearRaceOptions(message) {
  racecourse.replaceChildren(new Option(message, ""));
  raceNumber.replaceChildren(new Option("--", ""));
}

function updateRaceNumberOptions(preferredNumber = raceNumber.value) {
  if (!availableSchedule) {
    raceNumber.replaceChildren(new Option("--", ""));
    return;
  }
  const races = availableSchedule.get(racecourse.value) || [];
  raceNumber.replaceChildren(...races.map((race) => new Option(
    `${race.race_number}R ${race.start_time}`,
    String(race.race_number),
    false,
    String(race.race_number) === String(preferredNumber)
  )));
  if (!raceNumber.value && races.length) {
    const preferred = races.find((race) => race.race_number === 11) || races[0];
    raceNumber.value = String(preferred.race_number);
  }
}

async function loadRaceSchedule(date, preferredCourse, preferredNumber) {
  const requestId = ++scheduleRequestId;
  try {
    let racecourses = scheduleCache.get(date)?.racecourses;
    if (!racecourses || scheduleCache.get(date).expiresAt < Date.now()) {
      const response = await fetch(`/api/schedule?date=${encodeURIComponent(date)}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message || "開催一覧を取得できませんでした。");
      racecourses = payload.racecourses || [];
      if (!racecourses.length) throw new Error("この日の開催情報はありません。");
      scheduleCache.set(date, {racecourses, expiresAt: Date.now() + SCHEDULE_CACHE_TTL_MS});
    }
    if (requestId !== scheduleRequestId) return;
    availableSchedule = new Map(racecourses.map((course) => [course.name, course.races || []]));
    racecourse.replaceChildren(...racecourses.map((course) => new Option(course.name, course.name)));
    racecourse.value = availableSchedule.has(preferredCourse) ? preferredCourse : racecourses[0].name;
    updateRaceNumberOptions(preferredNumber);
    clearError();
  } catch (error) {
    if (requestId !== scheduleRequestId) return;
    availableSchedule = null;
    clearRaceOptions("開催情報なし");
    showError(error.message || "開催一覧を取得できませんでした。");
  }
  saveUiState();
}

clearRaceOptions("開催情報を読み込み中");
const restoredUiState = restoreUiState();
raceDate.addEventListener("change", () => loadRaceSchedule(raceDate.value, racecourse.value, raceNumber.value));
racecourse.addEventListener("change", () => updateRaceNumberOptions());
loadRaceSchedule(raceDate.value, restoredUiState?.racecourse || racecourse.value, restoredUiState?.race_number || raceNumber.value);
form.addEventListener("change", saveUiState);
form.addEventListener("input", saveUiState);
window.addEventListener("beforeunload", saveUiState);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/service-worker.js").catch(() => {});
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  setAnalysisLoading(true);
  setProgress("レース分析を実行中です");
  try {
    const requestPayload = {
      race_date: raceDate.value,
      racecourse: racecourse.value,
      race_number: Number(raceNumber.value),
      iterations: 10000,
      seed: 20260816,
      mode: "jra"
    };
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(requestPayload)
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || "JRA公式情報を取得できませんでした。");
    current = payload;
    current.request = requestPayload;
    saveLastAnalysisState();
    renderAll();
    selectTab(payload.result_only ? "review" : "analysis");
  } catch (error) {
    current = null;
    tabs.classList.add("hidden");
    summary.classList.add("hidden");
    showError(error.message || "分析に失敗しました。");
  } finally {
    progress.classList.add("hidden");
    setAnalysisLoading(false);
  }
});

openHistoryButton.addEventListener("click", () => {
  clearError();
  tabs.classList.remove("hidden");
  summary.classList.remove("hidden");
  summary.innerHTML = `<strong>保存履歴</strong><br><span class="meta">保存済みレースと学習状況を表示しています。</span>`;
  if (!current) renderAnalysisRequiredPanels();
  renderLearning();
  renderHistory();
  selectTab("history");
});

exportDataButton.addEventListener("click", exportData);
importDataButton.addEventListener("click", () => importFile.click());
importFile.addEventListener("change", importData);
diagnoseProviderButton.addEventListener("click", diagnoseProvider);
restoreLastAnalysis();
window.setInterval(() => {
  const activeTab = document.querySelector(".tabs button.active")?.dataset.tab;
  if (activeTab === "review" && document.visibilityState === "visible") refreshCurrentSavedResult();
}, RESULT_REFRESH_INTERVAL_MS);

tabs.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  selectTab(button.dataset.tab);
});

function renderAll() {
  tabs.classList.remove("hidden");
  summary.classList.remove("hidden");
  summary.innerHTML = `
    <div class="race-summary-main">
      <div><span class="race-kicker">${current.race.racecourse.value} ${current.race.race_number.value}R</span><strong>${current.race.race_name.value}</strong></div>
      <span class="race-condition">${current.race.surface.value}${current.race.distance.value}m</span>
    </div>
    <div class="meta">${current.result_only ? "発走済みのため予測を省略し、公式結果を表示しています。" : `${current.race.race_date.value} / ${parseQualityLabel(current.parse_quality)}${current.provider_name ? ` / ${current.provider_name}` : ""}`}</div>
  `;
  renderEntries();
  renderAnalysis();
  renderSimulation();
  renderUserPredictions();
  renderReview();
  renderLearning();
  renderSnapshots();
  renderHistory();
}

async function exportData() {
  clearError();
  try {
    const response = await fetch("/api/export");
    if (!response.ok) throw new Error("export failed");
    const payload = await response.json();
    const blob = new Blob([JSON.stringify(payload, null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `horseprediction-export-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    showError("データを書き出しました。", false);
  } catch {
    showError("データ出力に失敗しました。");
  }
}

async function diagnoseProvider() {
  clearError();
  const requestPayload = {
    race_date: raceDate.value,
    racecourse: racecourse.value,
    race_number: Number(raceNumber.value),
    mode: "jra"
  };
  setProgress("データ取得を診断中です");
  try {
    const response = await fetch("/api/provider-diagnostics", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(requestPayload)
    });
    const payload = await response.json();
    progress.classList.add("hidden");
    if (!response.ok) throw new Error(payload.message || "取得診断に失敗しました。");
    showError(`取得診断OK: ${payload.race_id} / ${payload.entry_count}頭 / ${parseQualityLabel(payload.parse_quality)}`, false);
  } catch (error) {
    progress.classList.add("hidden");
    showError(error.message || "取得診断に失敗しました。");
  }
}

async function importData() {
  clearError();
  const file = importFile.files?.[0];
  importFile.value = "";
  if (!file) return;
  try {
    const payload = JSON.parse(await file.text());
    const response = await fetch("/api/import", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || "import failed");
    const message = importSummary(result.result);
    showError(`データを読み込みました。${message}`, false);
    current = null;
    tabs.classList.remove("hidden");
    summary.classList.remove("hidden");
    summary.innerHTML = `<strong>データ読込完了</strong><br><span class="meta">${message}</span>`;
    renderAnalysisRequiredPanels();
    renderLearning();
    renderHistory();
    selectTab("history");
  } catch (error) {
    showError(error.message || "データ読込に失敗しました。");
  }
}

function renderAnalysisRequiredPanels() {
  const message = `<article class="analysis-card">レースを分析すると表示されます。</article>`;
  document.querySelector("#entries").innerHTML = message;
  document.querySelector("#analysis").innerHTML = message;
  document.querySelector("#simulation").innerHTML = message;
  document.querySelector("#user").innerHTML = message;
  document.querySelector("#review").innerHTML = message;
  document.querySelector("#snapshots").innerHTML = message;
}

function renderEntries() {
  document.querySelector("#entries").innerHTML = current.entries.map((entry) => `
    <article class="analysis-card">
      <div class="row-head"><span class="horse-number">${entry.horse_number.value} ${entry.horse.horse_name.value}</span><span>${entry.frame_number.value}枠</span></div>
      <div class="meta">騎手 ${entry.jockey_name.value ?? "unknown"} / 斤量 ${entry.carried_weight.value ?? "unknown"} / ${entry.sex.value ?? ""}${entry.age.value ?? ""}</div>
      <div class="meta">${marketInfo(entry)}</div>
    </article>
  `).join("");
}

function renderAnalysis() {
  if (!current.predictions.length) {
    document.querySelector("#analysis").innerHTML = `<article class="analysis-card">${current.result_only ? "発走済みのレースのため、事後予測は作成していません。結果タブに公式着順を表示しています。" : "保存済みAI予測はありません。"}</article>`;
    return;
  }
  const entriesByNumber = new Map(current.entries.map((entry) => [Number(entry.horse_number.value), entry]));
  document.querySelector("#analysis").innerHTML = current.predictions.map((prediction, index) => {
    const entry = entriesByNumber.get(Number(prediction.horse_number));
    const popularity = entry?.popularity?.value;
    const popularityNumber = Number(popularity);
    const popularityLabel = Number.isInteger(popularityNumber) && popularityNumber > 0
      ? `${popularityNumber}番人気`
      : "-";
    const positive = limitedReasons(prediction.explanation.positive).slice(0, 3);
    const negative = limitedReasons(prediction.explanation.negative).slice(0, 3);
    return `
      <article class="prediction prediction-rank-${Math.min(index + 1, 4)}">
        <div class="prediction-rank"><span>${index + 1}</span><small>${rankingLabel(index)}</small></div>
        <div class="prediction-body">
          <div class="prediction-head">
            <span class="horse-number"><b>${prediction.horse_number}</b> ${prediction.horse_name}</span>
          </div>
          <div class="prediction-quick-metrics">
            <span>勝率 <b>${percent(prediction.win_probability)}</b></span>
            <span>人気 <b>${popularityLabel}</b></span>
            ${expectedValueMetric(prediction)}
          </div>
          ${(positive.length || negative.length) ? `
            <details class="reason-details">
            <summary>${materialIcon("lightbulb")}評価理由</summary>
              <div class="factors">
                ${positive.length ? `<div><strong>プラス</strong><ul>${positive.map((x) => `<li>${x}</li>`).join("")}</ul></div>` : ""}
                ${negative.length ? `<div><strong>注意</strong><ul>${negative.map((x) => `<li>${x}</li>`).join("")}</ul></div>` : ""}
              </div>
            </details>
          ` : ""}
        </div>
      </article>
    `;
  }).join("");
}

function renderSimulation() {
  const sim = current.simulation;
  if (!sim) {
    document.querySelector("#simulation").innerHTML = `<article class="analysis-card">保存済みSimulationはありません。</article>`;
    return;
  }
  const topSimulationWinRate = Math.max(...sim.ranking.map((item) => Number(item.win_rate)), 0);
  document.querySelector("#simulation").innerHTML = `
    <article class="analysis-card"><strong>${sim.iterations.toLocaleString()}回 / seed ${sim.seed}</strong></article>
    ${decisionSummaryRow(sim.decision_summary)}
    ${candidateGroupRows(sim.candidate_groups)}
    ${scenarioConsensusRows(sim.scenario_consensus)}
    ${selectionNoteRows(sim.selection_notes || [])}
    ${sim.ranking.map((item) => `
      <article class="analysis-card simulation-rank">
        <div class="row-head"><strong>${item.simulation_rank ?? ""}位 ${item.horse_number} ${item.horse_name}</strong><strong>${percent(item.win_rate)}</strong></div>
        <div class="probability-bar"><span style="width:${topSimulationWinRate > 0 ? Math.max(4, Math.round(Number(item.win_rate) / topSimulationWinRate * 100)) : 0}%"></span></div>
        <div class="meta">連対 ${percent(item.top2_rate)} / 複勝圏 ${percent(item.top3_rate)} / 最頻 ${item.most_likely_position ?? "-"}着 ${percent(item.most_likely_position_rate)}</div>
      </article>
    `).join("")}
    ${sim.scenarios.map((item) => `<article class="scenario"><strong>${item.name}</strong><div class="meta">${item.note}</div><div>${item.top_horse_numbers.join(" → ")}</div></article>`).join("")}
  `;
}

function renderUserPredictions() {
  document.querySelector("#user").innerHTML = `
    <article class="analysis-card">
      <button id="save-user" class="btn btn-primary has-icon">${materialIcon("star")}<span>予想を保存</span></button>
      <button id="draft-ai-user" class="btn btn-outline-secondary secondary-button has-icon" type="button">${materialIcon("sparkles")}<span>AI下書き反映</span></button>
      <div class="meta">自分の印だけを保存します。</div>
    </article>
    <article id="user-quality" class="analysis-card"></article>
    ${current.entries.map((entry) => userPredictionCard(entry)).join("")}
  `;
  document.querySelector("#save-user").addEventListener("click", saveUserPredictions);
  document.querySelector("#draft-ai-user").addEventListener("click", applyAiUserDraft);
  document.querySelectorAll(".user-prediction select").forEach((input) => {
    input.addEventListener("input", renderUserPredictionQuality);
    input.addEventListener("change", renderUserPredictionQuality);
  });
  renderUserPredictionQuality();
}

async function saveUserPredictions() {
  const predictions = collectUserPredictions();
  const response = await fetch(`/api/user-predictions/${current.race.race_id}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({predictions})
  });
  const payload = await response.json();
  if (!response.ok) {
    showError(payload.message || "予想を保存できませんでした。");
    return;
  }
  current.latest_user_predictions = meaningfulUserPredictions(predictions);
  saveLastAnalysisState();
  showError(`予想を保存しました。${payload.saved}頭分`, false);
}

function renderReview() {
  document.querySelector("#review").innerHTML = `
    ${resultStatusMarkup()}
    <article class="analysis-card">
      <button id="fetch-result" class="btn btn-primary has-icon" type="button">${materialIcon("download")}<span>結果を取得</span></button>
      <button id="update-result" class="btn btn-primary has-icon">${materialIcon("refresh")}<span>結果を更新してレビュー</span></button>
      <button id="review-saved-result" class="btn btn-outline-secondary secondary-button has-icon" type="button">${materialIcon("clipboard")}<span>保存済み結果でレビュー</span></button>
    </article>
    <div id="result-grid" class="result-grid">${resultEntriesMarkup()}</div>
    <div id="review-output"></div>
  `;
  document.querySelector("#fetch-result").addEventListener("click", fetchResultFromProvider);
  document.querySelector("#update-result").addEventListener("click", async () => {
    const result = await fetchResultFromProvider();
    if (!result) return;
    const response = await fetch(`/api/review/${current.race.race_id}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...current.request, predictions: collectUserPredictions(), use_saved_result: true})
    });
    if (!response.ok) {
      document.querySelector("#review-output").innerHTML = `<article class="review-row">レビューを作成できませんでした。</article>`;
      return;
    }
    const review = await response.json();
    current.result_source = review.result_source;
    fillResultPositions(review.rows);
    document.querySelector("#review-output").innerHTML = `
      <article class="review-row">AI Brier ${review.ai_brier_score} / AI LogLoss ${review.ai_log_loss}<br>AI予測 ${predictionSourceLabel(review.prediction_source)} / 自分予想 ${userPredictionSourceLabel(review.user_prediction_source)} / ${review.note}</article>
      <article class="review-row"><strong>${comparisonLabel(review.comparison)}</strong><div class="meta">${review.comparison?.note ?? ""}</div></article>
      ${reviewInsightRows(review.insights)}
      ${review.rows.map((row) => `<article class="review-row">${row.horse_number} ${row.horse_name} / AI ${percent(row.ai_probability)} / 結果 ${resultLabel(row)}</article>`).join("")}
    `;
    renderLearning();
    renderHistory();
  });
  document.querySelector("#review-saved-result").addEventListener("click", reviewSavedResult);
}

async function fetchResultFromProvider() {
  const button = document.querySelector("#fetch-result");
  if (button) {
    button.disabled = true;
    button.textContent = "取得中";
  }
  try {
    const response = await fetch(`/api/results/${current.race.race_id}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(current.request)
    });
    const payload = await response.json();
    if (!response.ok) {
      document.querySelector("#review-output").innerHTML = `<article class="review-row">${payload.message || "結果を取得できませんでした。未確定、掲載期間外、またはJRA公式側の構造変更の可能性があります。"}</article>`;
      return null;
    }
    current.result_source = payload.result_source || "provider";
    applyResultToForm(payload.result);
    const message = String(current.result_source).startsWith("saved_")
      ? "保存済みのJRA結果を表示しました。"
      : "JRA公式結果を取得して保存しました。";
    document.querySelector("#review-output").innerHTML = `<article class="review-row">${message}</article>`;
    renderHistory();
    return payload.result;
  } catch {
    document.querySelector("#review-output").innerHTML = `<article class="review-row">結果取得時に通信エラーが発生しました。</article>`;
    return null;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "結果を取得";
    }
  }
}

async function diagnoseResult() {
  const response = await fetch(`/api/result-diagnostics/${current.race.race_id}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(current.request)
  });
  const payload = await response.json();
  if (!response.ok) {
    document.querySelector("#review-output").innerHTML = `<article class="review-row">${payload.message || "結果診断に失敗しました。"}</article>`;
    return;
  }
  document.querySelector("#review-output").innerHTML = `<article class="review-row">結果診断OK: ${payload.entry_count}頭 / 1着 ${payload.winner_count}頭 / ${payload.result_timestamp ?? ""}</article>`;
}

async function reviewSavedResult() {
  const response = await fetch(`/api/review/${current.race.race_id}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({...current.request, predictions: collectUserPredictions(), use_saved_result: true})
  });
  if (!response.ok) {
    document.querySelector("#review-output").innerHTML = `<article class="review-row">保存済み結果でレビューできませんでした。結果が未保存の可能性があります。</article>`;
    return;
  }
  const review = await response.json();
  current.result_source = review.result_source;
  fillResultPositions(review.rows);
  document.querySelector("#review-output").innerHTML = `
    <article class="review-row">AI Brier ${review.ai_brier_score} / 結果 ${resultSourceLabel(review.result_source)}<br>AI予測 ${predictionSourceLabel(review.prediction_source)} / 自分予想 ${userPredictionSourceLabel(review.user_prediction_source)} / ${review.note}</article>
    <article class="review-row"><strong>${comparisonLabel(review.comparison)}</strong><div class="meta">${review.comparison?.note ?? ""}</div></article>
    ${reviewInsightRows(review.insights)}
    ${review.rows.map((row) => `<article class="review-row">${row.horse_number} ${row.horse_name} / AI ${percent(row.ai_probability)} / 結果 ${resultLabel(row)}</article>`).join("")}
  `;
  renderLearning();
  renderHistory();
}

async function renderLearning() {
  const panel = document.querySelector("#learning");
  panel.innerHTML = `<article class="analysis-card">学習状況を読み込み中</article>`;
  try {
    const response = await fetch("/api/learning");
    const summary = await response.json();
    panel.innerHTML = `
      <article class="analysis-card">
        <strong>レビュー済み ${summary.review_count}レース</strong>
        <div class="metrics">
          <div class="metric">AI Brier<b>${summary.ai_brier_score ?? "-"}</b></div>
          <div class="metric">User Brier<b>${summary.user_brier_score ?? "-"}</b></div>
          <div class="metric">馬別条件<b>${summary.horse_breakdown.length}</b></div>
        </div>
        <div class="meta">${summary.note}</div>
      </article>
      <article class="analysis-card">
        <strong>AI/User比較</strong>
        <div class="metrics">
          <div class="metric">AI優勢<b>${summary.comparison_summary.ai_wins}</b></div>
          <div class="metric">自分優勢<b>${summary.comparison_summary.user_wins}</b></div>
          <div class="metric">平均差<b>${summary.comparison_summary.average_brier_delta ?? "-"}</b></div>
        </div>
        <div class="meta">${summary.comparison_summary.note}</div>
      </article>
      <article class="analysis-card">
        <strong>直近トレンド</strong>
        <div class="metrics">
          <div class="metric">直近件数<b>${summary.trend_summary.recent_count}</b></div>
          <div class="metric">AI直近<b>${summary.trend_summary.ai_recent_brier_score ?? "-"}</b></div>
          <div class="metric">User直近<b>${summary.trend_summary.user_recent_brier_score ?? "-"}</b></div>
        </div>
        <div class="meta">AI ${trendLabel(summary.trend_summary.ai_direction)} / 自分 ${trendLabel(summary.trend_summary.user_direction)} / ${summary.trend_summary.note}</div>
      </article>
      <article class="analysis-card">
        <strong>AI Top5評価</strong>
        <div class="metrics">
          <div class="metric">評価レース<b>${summary.ranking_metric_summary.top5_race_count}</b></div>
          <div class="metric">1着Top5率<b>${percent(summary.ranking_metric_summary.winner_in_top5_rate)}</b></div>
          <div class="metric">平均捕捉<b>${summary.ranking_metric_summary.average_top5_capture_count ?? "-"} / 5</b></div>
          <div class="metric">4/5以上<b>${percent(summary.ranking_metric_summary.top5_capture_4of5_rate)}</b></div>
          <div class="metric">5/5<b>${percent(summary.ranking_metric_summary.top5_capture_5of5_rate)}</b></div>
        </div>
        <div class="meta">捕捉率 ${percent(summary.ranking_metric_summary.average_top5_capture_rate)} / ${summary.ranking_metric_summary.top5_sample_status === "sample_small" ? "標本少（100レース未満）" : "集計可"}</div>
      </article>
      <article class="analysis-card">
        <strong>自分予想</strong>
        <div class="meta">画面入力は印だけにしています。勝率や各評価は表示・入力しません。</div>
      </article>
      <article class="analysis-card">
        <strong>解析品質</strong>
        <div class="metrics">
          <div class="metric">解析件数<b>${summary.data_quality_summary.analysis_run_count}</b></div>
          <div class="metric">特徴量充足<b>${ratioLabel(summary.data_quality_summary.average_available_feature_ratio)}</b></div>
          <div class="metric">過去走不足<b>${summary.data_quality_summary.average_missing_history_count ?? "-"}</b></div>
        </div>
        <div class="meta">parse ${statusCountsLabel(summary.data_quality_summary.parse_status_counts)} / provider ${statusCountsLabel(summary.data_quality_summary.provider_counts)}</div>
        <div class="meta">欠損上位 ${featureCountsLabel(summary.data_quality_summary.top_unavailable_features)}</div>
        <div class="meta">${summary.data_quality_summary.note}</div>
      </article>
      ${attentionRunRows(summary.data_quality_summary.attention_runs)}
      <section id="learning-detail" class="history-detail hidden"></section>
      <article class="analysis-card">
        <strong>確率校正</strong>
        <div class="meta">${summary.calibration_summary.note}</div>
      </article>
      ${learningInsightRows(summary.learning_insights)}
      ${calibrationRows("AI", summary.calibration_summary.ai)}
      ${calibrationRows("自分", summary.calibration_summary.user)}
      ${summary.condition_breakdown.map((item) => `
        <article class="review-row">
          <strong>${conditionLabel(item.dimension)}: ${item.value}</strong>
          <div class="meta">${item.count}件 / AI ${item.ai_brier_score ?? "-"} / User ${item.user_brier_score ?? "-"} / ${item.statistical_note === "sample_small" ? "標本少" : "集計可"}</div>
        </article>
      `).join("")}
      ${summary.horse_breakdown.map((item) => `
        <article class="review-row">
          <strong>${horseConditionLabel(item.dimension)}: ${horseConditionValue(item.dimension, item.value)}</strong>
          <div class="meta">${item.count}頭 / AI ${item.ai_brier_score ?? "-"} / User ${item.user_brier_score ?? "-"} / ${item.statistical_note === "sample_small" ? "標本少" : "集計可"}</div>
        </article>
      `).join("")}
      <article class="analysis-card"><strong>直近レビュー</strong></article>
      ${summary.recent_reviews.map((item) => `
        <article class="review-row">
          <div class="row-head">
            <strong>${reviewRaceLabel(item)}</strong>
            <span class="meta">${comparisonLabel(item.comparison)}</span>
          </div>
          <div class="meta">AI ${item.ai_brier_score ?? "-"} / User ${item.user_brier_score ?? "-"} / ${item.created_at ?? ""}</div>
          <button class="inline-button" data-learning-detail="${item.race_id}">詳細</button>
        </article>
      `).join("")}
    `;
    panel.querySelectorAll("[data-learning-detail]").forEach((button) => {
      button.addEventListener("click", () => renderLearningDetail(button.dataset.learningDetail));
    });
  } catch {
    panel.innerHTML = `<article class="analysis-card">学習状況を取得できませんでした。</article>`;
  }
}

async function renderSnapshots() {
  const panel = document.querySelector("#snapshots");
  panel.innerHTML = `<article class="analysis-card">保存済みAI予測を読み込み中</article>`;
  try {
    const response = await fetch(`/api/prediction-snapshots/${current.race.race_id}?limit=${current.predictions.length}`);
    const payload = await response.json();
    const snapshots = payload.snapshots
      .slice()
      .sort((a, b) => Number(a.horse_number) - Number(b.horse_number));
    if (!snapshots.length) {
      panel.innerHTML = `<article class="analysis-card">保存済みAI予測はまだありません。</article>`;
      return;
    }
    panel.innerHTML = `
      <article class="analysis-card">
        <strong>直近保存 ${snapshots.length}頭</strong>
        <div class="meta">seed ${snapshots[0].simulation_seed} / ${snapshots[0].model_version} / ${snapshots[0].feature_version}</div>
        <div class="meta">${snapshots[0].prediction_timestamp ?? ""}</div>
      </article>
      ${snapshots.map((snapshot) => {
        const popularity = Number(snapshot.raw_features?.popularity);
        const popularityLabel = Number.isFinite(popularity) && popularity > 0 ? `${popularity}番人気` : "-";
        return `
        <article class="review-row">
          <strong>${snapshot.horse_number} ${snapshot.prediction.horse_name}</strong>
          <div class="metrics">
            <div class="metric">勝率<b>${percent(snapshot.ai_win_probability)}</b></div>
            <div class="metric">人気<b>${popularityLabel}</b></div>
            ${expectedValueMetric(snapshot.prediction || {})}
          </div>
          <div class="meta">${featureDigest(snapshot.raw_features)}</div>
        </article>
      `;
      }).join("")}
    `;
  } catch {
    panel.innerHTML = `<article class="analysis-card">保存済みAI予測を取得できませんでした。</article>`;
  }
}

async function renderHistory() {
  const panel = document.querySelector("#history");
  panel.innerHTML = `<article class="analysis-card">保存履歴を読み込み中</article>`;
  try {
    const response = await fetch(`/api/races?${historyQuery()}`);
    const payload = await response.json();
    const controls = historyFilterControls();
    if (!payload.races.length) {
      panel.innerHTML = `
        ${controls}
        <section id="history-detail" class="history-detail hidden"></section>
        <article class="analysis-card">条件に合う保存済みレースはまだありません。</article>
      `;
      bindHistoryFilters(panel);
      return;
    }
    panel.innerHTML = `
      ${controls}
      <section id="history-detail" class="history-detail hidden"></section>
      ${payload.races.map((race) => `
        <article class="review-row">
          <div class="row-head">
            <strong>${race.race_date} ${race.racecourse} ${race.race_number}R ${race.race_name ?? ""}</strong>
            <span class="meta">${race.snapshot_count} snapshots</span>
          </div>
          <div class="meta">${race.surface ?? "-"}${race.distance ?? "-"}m / ${race.track_condition ?? "-"} / ${race.entry_count}頭</div>
          <div class="metrics">
            <div class="metric">結果<b>${race.has_result ? resultSourceLabel(race.result_source) : "未保存"}</b></div>
            <div class="metric">レビュー<b>${race.review_count}件</b></div>
            <div class="metric">AI Brier<b>${race.latest_ai_brier_score ?? "-"}</b></div>
          </div>
          <div class="meta">解析 ${race.parse_status ?? "-"} / issues ${race.parse_issue_count ?? "-"} / ${race.provider_name ?? "-"}</div>
          <div class="meta">過去走不足 ${race.missing_history_count ?? "-"}頭 / 特徴量充足 ${ratioLabel(race.available_feature_ratio)}</div>
          <button class="inline-button" data-history-detail="${race.race_id}">詳細</button>
        </article>
      `).join("")}
    `;
    bindHistoryFilters(panel);
    panel.querySelectorAll("[data-history-detail]").forEach((button) => {
      button.addEventListener("click", () => renderHistoryDetail(button.dataset.historyDetail));
    });
  } catch {
    panel.innerHTML = `<article class="analysis-card">保存履歴を取得できませんでした。</article>`;
  }
}

async function renderHistoryDetail(raceId) {
  const detailPanel = document.querySelector("#history-detail");
  detailPanel.classList.remove("hidden");
  detailPanel.innerHTML = `<article class="analysis-card">詳細を読み込み中</article>`;
  try {
    const response = await fetch(`/api/races/${raceId}`);
    if (!response.ok) throw new Error("detail failed");
    const detail = await response.json();
    const race = detail.race;
    const predictions = detail.latest_predictions || [];
    const userPredictions = detail.latest_user_predictions || {};
    const analysisRun = detail.latest_analysis_run;
    const featureQuality = analysisRun?.payload?.feature_quality || {};
    const reviewRows = detail.latest_review?.rows || [];
    detailPanel.innerHTML = `
      <article class="analysis-card">
        <div class="row-head">
          <strong>${race.race_date.value} ${race.racecourse.value} ${race.race_number.value}R ${race.race_name.value ?? ""}</strong>
          <span class="meta">${detail.latest_review ? "レビュー済" : "未レビュー"}</span>
        </div>
        <div class="meta">${race.surface.value ?? "-"}${race.distance.value ?? "-"}m / 天候 ${race.weather.value ?? "-"} / 馬場 ${race.track_condition.value ?? "-"} / ${detail.entries.length}頭</div>
        <div class="meta">解析 ${analysisRun?.parse_status ?? "-"} / issues ${analysisRun?.issue_count ?? "-"} / ${analysisRun?.provider_name ?? "-"}</div>
        <div class="meta">過去走不足 ${featureQuality.missing_history_count ?? "-"}頭 / 特徴量充足 ${ratioLabel(featureQuality.available_feature_ratio)}</div>
        <button class="inline-button" id="open-saved-analysis">この保存分析を開く</button>
      </article>
      ${predictions.map((prediction) => `
        <article class="review-row">
          <div class="row-head">
            <strong>${prediction.horse_number} ${prediction.horse_name}</strong>
          </div>
          <div class="metrics">
            <div class="metric">勝率<b>${percent(prediction.win_probability)}</b></div>
            <div class="metric">連対率<b>${percent(prediction.top2_probability)}</b></div>
            <div class="metric">複勝圏<b>${percent(prediction.top3_probability)}</b></div>
          </div>
          <div class="meta">自分予想 ${userPredictionDigest(userPredictions[prediction.horse_number])}</div>
        </article>
      `).join("")}
      ${detail.result ? `<article class="analysis-card"><strong>結果</strong><div class="meta">${resultSourceLabel(detail.result_source)} / ${resultSummary(detail.result.entries)}</div></article>` : ""}
      ${detail.latest_review ? `
        <article class="analysis-card">
          <strong>直近レビュー</strong>
          <div class="meta">AI Brier ${detail.latest_review.ai_brier_score ?? "-"} / User Brier ${detail.latest_review.user_brier_score ?? "-"} / 結果 ${resultSourceLabel(detail.result_source)}</div>
          <div class="meta">${comparisonLabel(detail.latest_review.comparison)}</div>
        </article>
        ${reviewInsightRows(detail.latest_review.insights)}
        ${reviewRows.slice(0, 5).map((row) => `<article class="review-row">${row.horse_number} ${row.horse_name} / AI ${percent(row.ai_probability)} / 結果 ${resultLabel(row)}</article>`).join("")}
      ` : ""}
    `;
    document.querySelector("#open-saved-analysis").addEventListener("click", () => openSavedAnalysis(detail));
  } catch {
    detailPanel.innerHTML = `<article class="analysis-card">詳細を取得できませんでした。</article>`;
  }
}

async function renderLearningDetail(raceId) {
  const detailPanel = document.querySelector("#learning-detail");
  detailPanel.classList.remove("hidden");
  detailPanel.innerHTML = `<article class="analysis-card">詳細を読み込み中</article>`;
  try {
    const response = await fetch(`/api/races/${raceId}`);
    if (!response.ok) throw new Error("detail failed");
    const detail = await response.json();
    const race = detail.race;
    const predictions = detail.latest_predictions || [];
    const reviewRows = detail.latest_review?.rows || [];
    detailPanel.innerHTML = `
      <article class="analysis-card">
        <div class="row-head">
          <strong>${race.race_date.value} ${race.racecourse.value} ${race.race_number.value}R ${race.race_name.value ?? ""}</strong>
          <span class="meta">${detail.latest_review ? "レビュー済" : "未レビュー"}</span>
        </div>
        <div class="meta">${race.surface.value ?? "-"}${race.distance.value ?? "-"}m / 天候 ${race.weather.value ?? "-"} / 馬場 ${race.track_condition.value ?? "-"} / ${detail.entries.length}頭</div>
        <button class="inline-button" id="open-learning-analysis">この保存分析を開く</button>
      </article>
      ${predictions.slice(0, 5).map((prediction) => `
        <article class="review-row">
          <div class="row-head">
            <strong>${prediction.horse_number} ${prediction.horse_name}</strong>
          </div>
          <div class="meta">勝率 ${percent(prediction.win_probability)} / 連対 ${percent(prediction.top2_probability)} / 複勝圏 ${percent(prediction.top3_probability)}</div>
        </article>
      `).join("")}
      ${detail.latest_review ? `
        <article class="analysis-card">
          <strong>直近レビュー</strong>
          <div class="meta">AI Brier ${detail.latest_review.ai_brier_score ?? "-"} / User Brier ${detail.latest_review.user_brier_score ?? "-"}</div>
          <div class="meta">${comparisonLabel(detail.latest_review.comparison)}</div>
        </article>
        ${reviewRows.slice(0, 5).map((row) => `<article class="review-row">${row.horse_number} ${row.horse_name} / AI ${percent(row.ai_probability)} / 結果 ${resultLabel(row)}</article>`).join("")}
      ` : ""}
    `;
    document.querySelector("#open-learning-analysis").addEventListener("click", () => openSavedAnalysis(detail));
  } catch {
    detailPanel.innerHTML = `<article class="analysis-card">詳細を取得できませんでした。</article>`;
  }
}

function openSavedAnalysis(detail) {
  const race = detail.race;
  const savedState = loadLastAnalysisState();
  const savedRequest = savedState?.race_id === race.race_id ? savedState.request : null;
  current = {
    race,
    entries: detail.entries,
    predictions: detail.latest_predictions || [],
    simulation: detail.latest_simulation,
    result: detail.result,
    result_source: detail.result_source,
    parse_quality: detail.latest_analysis_run?.payload?.parse_quality || null,
    provider_name: detail.latest_analysis_run?.provider_name || null,
    latest_user_predictions: detail.latest_user_predictions || {},
    message: "保存済み分析を表示しています。",
    request: {
      race_date: race.race_date.value,
      racecourse: race.racecourse.value,
      race_number: Number(race.race_number.value),
      iterations: Number(savedRequest?.iterations ?? detail.latest_simulation?.iterations ?? 10000),
      seed: Number(savedRequest?.seed ?? detail.latest_simulation?.seed ?? 20260816),
      mode: "jra"
    }
  };
  raceDate.value = race.race_date.value;
  racecourse.value = race.racecourse.value;
  raceNumber.value = String(race.race_number.value);
  loadRaceSchedule(raceDate.value, racecourse.value, raceNumber.value);
  saveUiState();
  saveLastAnalysisState();
  renderAll();
  selectTab("analysis");
  showError("保存済み分析を開きました。", false);
}

function saveLastAnalysisState() {
  if (!current?.race?.race_id) return;
  try {
    localStorage.setItem(
      LAST_ANALYSIS_KEY,
      JSON.stringify({
        race_id: current.race.race_id,
        request: current.request || null,
        saved_on: todayValue
      })
    );
  } catch {
  }
}

function loadLastAnalysisState() {
  try {
    return JSON.parse(localStorage.getItem(LAST_ANALYSIS_KEY) || "null");
  } catch {
    return null;
  }
}

async function restoreLastAnalysis() {
  const saved = loadLastAnalysisState();
  if (!saved?.race_id) return;
  const uiState = loadUiState();
  if (saved.request && String(saved.request.race_date) !== todayValue) return;
  if (uiState && !analysisRequestMatchesUi(saved.request, {...uiState, race_date: todayValue})) return;
  const activeTab = uiState?.active_tab;
  try {
    const response = await fetch(`/api/races/${saved.race_id}`);
    if (!response.ok) throw new Error("restore failed");
    const detail = await response.json();
    openSavedAnalysis(detail);
    if (activeTab) selectTab(activeTab);
    showError("前回の保存済み分析を復元しました。", false);
  } catch {
    try {
      localStorage.removeItem(LAST_ANALYSIS_KEY);
    } catch {
    }
  }
}

function analysisRequestMatchesUi(request, uiState) {
  if (!request) return true;
  return String(request.race_date) === String(uiState.race_date)
    && String(request.racecourse) === String(uiState.racecourse)
    && Number(request.race_number) === Number(uiState.race_number);
}

function savedResultLabel(horseNumber) {
  const entry = (current.result?.entries || []).find((item) => Number(item.horse_number) === Number(horseNumber));
  return entry ? resultLabel(entry) : "未取得";
}

function resultStatusMarkup() {
  const result = current.result;
  if (!result) {
    return `
      <article id="result-status" class="analysis-card result-status result-status-missing" data-result-status>
        <div class="row-head"><strong>結果未取得</strong><span>自動取得待ち</span></div>
        <div class="meta">JRA公式結果が保存されると着順を自動表示します。</div>
      </article>
    `;
  }
  const count = (result.entries || []).length;
  return `
    <article id="result-status" class="analysis-card result-status result-status-acquired" data-result-status>
      <div class="row-head"><strong>結果取得済み</strong><span>取得済み</span></div>
      <div class="meta">${resultSourceLabel(current.result_source)} / ${count}頭${result.result_timestamp ? ` / ${result.result_timestamp}` : ""}</div>
    </article>
  `;
}

function resultEntriesMarkup() {
  const savedEntries = current.result?.entries || [];
  const entries = savedEntries.length
    ? savedEntries.slice().sort((a, b) => resultSortValue(a) - resultSortValue(b))
    : current.entries.map((entry) => ({
        horse_number: entry.horse_number.value,
        horse_name: entry.horse.horse_name.value
      }));
  return entries.map((entry) => `
    <article class="analysis-card result-entry" data-horse-number="${entry.horse_number}">
      <div class="row-head">
        <strong>${entry.horse_number} ${entry.horse_name}</strong>
        <span class="result-position" data-result-position>${savedResultLabel(entry.horse_number)}</span>
      </div>
    </article>
  `).join("");
}

function resultSortValue(entry) {
  const finishPosition = Number(entry.finish_position);
  return Number.isFinite(finishPosition) && finishPosition > 0 ? finishPosition : 1000 + Number(entry.horse_number);
}

function applyResultToForm(result) {
  if (!result) return;
  current.result = result;
  const status = document.querySelector("#result-status");
  if (status) status.outerHTML = resultStatusMarkup();
  const grid = document.querySelector("#result-grid");
  if (grid) grid.innerHTML = resultEntriesMarkup();
}

function fillResultPositions(rows = []) {
  const labels = new Map(
    rows.map((row) => [Number(row.horse_number), resultLabel(row)])
  );
  document.querySelectorAll(".result-entry").forEach((card) => {
    const position = card.querySelector("[data-result-position]");
    if (!position) return;
    position.textContent = labels.get(Number(card.dataset.horseNumber)) ?? "未取得";
  });
}

async function refreshCurrentSavedResult() {
  if (!current?.race?.race_id) return;
  const raceId = current.race.race_id;
  const resultBeforeRequest = current.result;
  try {
    const response = await fetch(`/api/results/${raceId}`);
    if (!response.ok) return;
    const payload = await response.json();
    if (current?.race?.race_id !== raceId || current.result !== resultBeforeRequest) return;
    const wasMissing = !current.result;
    current.result_source = payload.result_source || "saved_provider";
    applyResultToForm(payload.result);
    if (wasMissing) {
      document.querySelector("#review-output").innerHTML = `<article class="review-row">保存済みのJRA結果を反映しました。</article>`;
    }
  } catch {
  }
}

function historyQuery() {
  const params = new URLSearchParams({limit: "50"});
  if (historyFilters.racecourse) params.set("racecourse", historyFilters.racecourse);
  if (historyFilters.hasResult) params.set("has_result", historyFilters.hasResult);
  if (historyFilters.hasReview) params.set("has_review", historyFilters.hasReview);
  return params.toString();
}

function historyFilterControls() {
  return `
    <article class="analysis-card">
      <div class="filter-grid">
      <select id="history-racecourse" class="form-select">
          ${["", "札幌", "函館", "福島", "新潟", "東京", "中山", "中京", "京都", "阪神", "小倉"].map((value) => `<option value="${value}"${historyFilters.racecourse === value ? " selected" : ""}>${value || "競馬場すべて"}</option>`).join("")}
        </select>
      <select id="history-result" class="form-select">
          <option value=""${historyFilters.hasResult === "" ? " selected" : ""}>結果すべて</option>
          <option value="true"${historyFilters.hasResult === "true" ? " selected" : ""}>結果あり</option>
          <option value="false"${historyFilters.hasResult === "false" ? " selected" : ""}>結果なし</option>
        </select>
      <select id="history-review" class="form-select">
          <option value=""${historyFilters.hasReview === "" ? " selected" : ""}>レビューすべて</option>
          <option value="true"${historyFilters.hasReview === "true" ? " selected" : ""}>レビューあり</option>
          <option value="false"${historyFilters.hasReview === "false" ? " selected" : ""}>レビューなし</option>
        </select>
        <button id="clear-history-filters" class="secondary-button" type="button">解除</button>
      </div>
    </article>
  `;
}

function bindHistoryFilters(panel) {
  panel.querySelector("#history-racecourse").addEventListener("change", (event) => {
    historyFilters.racecourse = event.target.value;
    renderHistory();
  });
  panel.querySelector("#history-result").addEventListener("change", (event) => {
    historyFilters.hasResult = event.target.value;
    renderHistory();
  });
  panel.querySelector("#history-review").addEventListener("change", (event) => {
    historyFilters.hasReview = event.target.value;
    renderHistory();
  });
  panel.querySelector("#clear-history-filters").addEventListener("click", () => {
    historyFilters.racecourse = "";
    historyFilters.hasResult = "";
    historyFilters.hasReview = "";
    renderHistory();
  });
}

function collectUserPredictions() {
  const predictions = {};
  document.querySelectorAll(".user-prediction").forEach((card) => {
    const horseNumber = card.dataset.horseNumber;
    predictions[horseNumber] = {
      mark: card.querySelector("[data-field='mark']").value || null
    };
  });
  return predictions;
}

function renderUserPredictionQuality() {
  const predictions = collectUserPredictions();
  const rows = Object.values(predictions);
  const markedCount = rows.filter((prediction) => prediction.mark).length;
  const panel = document.querySelector("#user-quality");
  if (!panel) return;
  panel.innerHTML = `
    <div class="row-head">
      <strong>${materialIcon("user")}入力サマリー</strong>
      <span class="meta">印 ${markedCount}/${current.entries.length}</span>
    </div>
    <div class="metrics">
      <div class="metric">印<b>${markedCount}</b></div>
    </div>
    <div class="meta">◎/○/▲/△/☆/消だけを保存します。</div>
  `;
}

function applyAiUserDraft() {
  const drafts = aiUserDrafts();
  let updated = 0;
  document.querySelectorAll(".user-prediction").forEach((card) => {
    const draft = drafts[Number(card.dataset.horseNumber)];
    if (!draft) return;
    updated += fillIfEmpty(card.querySelector("[data-field='mark']"), draft.mark) ? 1 : 0;
  });
  renderUserPredictionQuality();
  showError(updated ? "AI下書きを空欄に反映しました。保存はまだしていません。" : "反映できる空欄はありませんでした。", false);
}

function aiUserDrafts() {
  const drafts = {};
  const groups = current.simulation?.candidate_groups || {};
  let assignments = [
    ...(groups.core?.horses || []).map((item, index) => ({...item, groupLabel: groups.core.label, mark: index === 0 ? "◎" : "○"})),
    ...(groups.opposition?.horses || []).map((item, index) => ({...item, groupLabel: groups.opposition.label, mark: index === 0 ? "▲" : "△"})),
    ...(groups.reserves?.horses || []).map((item) => ({...item, groupLabel: groups.reserves.label, mark: "☆"})),
  ];
  if (!assignments.length) {
    assignments = (current.predictions || [])
      .slice()
      .sort((a, b) => Number(b.win_probability) - Number(a.win_probability))
      .slice(0, 5)
      .map((item, index) => ({
        horse_number: item.horse_number,
        horse_name: item.horse_name,
        simulation_rank: index + 1,
        groupLabel: "AI予測",
        mark: ["◎", "○", "▲", "△", "☆"][index],
      }));
  }
  assignments.forEach((item) => {
    drafts[Number(item.horse_number)] = {
      mark: item.mark,
    };
  });
  return drafts;
}

function fillIfEmpty(input, value) {
  if (!input || value === null || value === undefined || value === "") return false;
  if (input.value !== "") return false;
  input.value = value;
  return true;
}

function setProgress(message) {
  progress.classList.remove("hidden");
  progress.innerHTML = `
    <div class="waiting-line"><span class="spinner" aria-hidden="true"></span><strong>${message}</strong></div>
  `;
}
function setAnalysisLoading(isLoading) {
  analyzeButton.disabled = isLoading;
  analyzeButton.setAttribute("aria-busy", String(isLoading));
  form.setAttribute("aria-busy", String(isLoading));
  analyzeButton.innerHTML = isLoading
    ? `<span class="spinner button-spinner" aria-hidden="true"></span><span>分析中</span>`
    : `${materialIcon("sparkles")}<span>分析開始</span>`;
}
function showError(message, isError = true) {
  errorBox.classList.remove("hidden");
  errorBox.textContent = message;
  errorBox.style.borderColor = isError ? "var(--danger)" : "var(--accent)";
  errorBox.style.color = isError ? "var(--danger)" : "var(--accent)";
}
function clearError() { errorBox.classList.add("hidden"); }
function selectTab(tabId) {
  const tabButtons = [...document.querySelectorAll(".tabs button")];
  const resolvedTabId = tabButtons.some((item) => item.dataset.tab === tabId) ? tabId : "analysis";
  tabButtons.forEach((item) => {
    const isActive = item.dataset.tab === resolvedTabId;
    item.classList.toggle("active", isActive);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("hidden", panel.id !== resolvedTabId));
  saveUiState();
  if (resolvedTabId === "review") refreshCurrentSavedResult();
}
function saveUiState() {
  try {
    localStorage.setItem(UI_STATE_KEY, JSON.stringify({
      race_date: raceDate.value,
      racecourse: racecourse.value,
      race_number: raceNumber.value,
      active_tab: document.querySelector(".tabs button.active")?.dataset.tab || "entries",
      saved_on: todayValue
    }));
  } catch {
  }
}
function loadUiState() {
  try {
    return JSON.parse(localStorage.getItem(UI_STATE_KEY) || "null");
  } catch {
    return null;
  }
}
function restoreUiState() {
  const saved = loadUiState();
  if (!saved) return;
  return saved;
}
function percent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return `${(Number(value) * 100).toFixed(1)}%`;
}
function expectedValueMetric(prediction) {
  const rawValue = prediction?.expected_value_index;
  if (rawValue === null || rawValue === undefined || rawValue === "") {
    return `<span class="expected-value" title="単勝オッズ未取得のため算出できません">期待値 <b>-</b></span>`;
  }
  const value = Number(rawValue);
  if (!Number.isFinite(value)) return `<span class="expected-value">期待値 <b>-</b></span>`;
  const adjusted = Boolean(prediction.expected_value_is_adjusted);
  const label = adjusted ? "補正期待値" : "期待値";
  const title = adjusted
    ? "単勝30倍以上のため、市場確率を基準にAI勝率を最大30%反映"
    : "AI勝率と単勝オッズから算出";
  const positiveClass = value >= 1 ? " expected-value-positive" : "";
  return `<span class="expected-value${positiveClass}" title="${title}">${label} <b>${value.toFixed(2)}</b></span>`;
}
function signedNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const numeric = Number(value);
  return `${numeric > 0 ? "+" : ""}${numeric}`;
}
function rankingLabel(index) {
  return ["本命", "対抗", "注目"][index] || "";
}
function marketInfo(entry) {
  const odds = entry?.win_odds?.value;
  const popularity = entry?.popularity?.value;
  const parts = [];
  if (odds !== null && odds !== undefined) parts.push(`単勝 ${Number(odds).toFixed(1)}`);
  if (popularity !== null && popularity !== undefined) parts.push(`${Number(popularity)}番人気`);
  return parts.length ? parts.join(" / ") : "単勝・人気 未取得";
}
function importSummary(result) {
  const imported = sumObject(result?.imported || {});
  const updated = sumObject(result?.updated || {});
  const skipped = sumObject(result?.skipped || {});
  return `追加 ${imported}件 / 更新 ${updated}件 / 重複skip ${skipped}件`;
}
function sumObject(value) {
  return Object.values(value).reduce((total, count) => total + Number(count || 0), 0);
}
function resultLabel(row) {
  if (row.finish_position) return `${row.finish_position}着${row.finish_time ? ` / ${row.finish_time}` : ""}`;
  if (row.result_status) return row.result_status;
  return row.actual_win ? "1着" : "未勝利";
}
function resultSummary(entries) {
  return entries
    .filter((entry) => entry.finish_position && entry.finish_position <= 3)
    .sort((a, b) => Number(a.finish_position) - Number(b.finish_position))
    .map((entry) => `${entry.finish_position}着 ${entry.horse_number} ${entry.horse_name}`)
    .join(" / ");
}
function parseQualityLabel(parseQuality) {
  if (!parseQuality) return "解析 -";
  return `解析 ${parseQuality.status} / ${parseQuality.recognized_entries}${parseQuality.expected_entries ? `/${parseQuality.expected_entries}` : ""}頭 / issues ${parseQuality.issues?.length ?? 0}`;
}
function ratioLabel(value) {
  return value == null ? "-" : percent(value);
}
function statusCountsLabel(counts) {
  const entries = Object.entries(counts || {});
  return entries.length ? entries.map(([key, value]) => `${key}:${value}`).join(" / ") : "-";
}
function featureCountsLabel(rows) {
  return rows?.length ? rows.map((item) => `${item.feature}:${item.count}`).join(" / ") : "-";
}
function predictionSourceLabel(source) {
  return source === "snapshot" ? "保存済みスナップショット" : "再計算";
}
function userPredictionSourceLabel(source) {
  return {
    "request+snapshot": "入力+保存済み",
    request: "入力値",
    snapshot: "保存済み",
    none: "なし"
  }[source] ?? "なし";
}
function resultSourceLabel(source) {
  return {
    manual: "手入力",
    provider: "取得結果",
    saved_manual: "保存済み手入力",
    saved_provider: "保存済み取得結果",
    saved: "保存済み"
  }[source] ?? "取得結果";
}
function userPredictionDigest(prediction) {
  if (!prediction) return "なし";
  return prediction.mark || "なし";
}
function reviewRaceLabel(item) {
  if (!item.race_date || !item.racecourse || !item.race_number) return item.race_id;
  return `${item.race_date} ${item.racecourse} ${item.race_number}R ${item.race_name ?? ""}`;
}
function comparisonLabel(comparison) {
  if (!comparison || !comparison.winner) return "AI/User比較なし";
  const label = {ai: "AI優勢", user: "自分予想優勢", tie: "同程度"}[comparison.winner] ?? "比較なし";
  return comparison.brier_delta == null ? label : `${label} / Brier差 ${comparison.brier_delta}`;
}
function reviewInsightRows(insights) {
  if (!insights) return "";
  const disagreements = insights.largest_disagreements || [];
  const aiMisses = insights.biggest_ai_misses || [];
  const userMisses = insights.biggest_user_misses || [];
  return `
    ${disagreements.length ? `<article class="review-row"><strong>AIと自分予想のズレ</strong><div class="meta">${disagreements.map((row) => `${row.horse_number} ${row.horse_name}: ${percent(row.difference)}`).join(" / ")}</div></article>` : ""}
    ${aiMisses.length ? `<article class="review-row"><strong>AIの大きな外し</strong><div class="meta">${aiMisses.map((row) => `${row.horse_number} ${row.horse_name}: 予測${row.predicted_rank ?? "-"}位 → ${resultLabel(row)} / 差${signedNumber(row.rank_error)}`).join(" / ")}</div></article>` : ""}
    ${userMisses.length ? `<article class="review-row"><strong>自分予想の大きな外し</strong><div class="meta">${userMisses.map((row) => `${row.horse_number} ${row.horse_name}: ${resultLabel(row)} / 自分 ${percent(row.user_probability)}`).join(" / ")}</div></article>` : ""}
  `;
}
function calibrationRows(label, rows) {
  if (!rows.length) return `<article class="review-row"><strong>${label}校正</strong><div class="meta">データなし</div></article>`;
  return rows.map((item) => `
    <article class="review-row">
      <strong>${label} ${item.bucket}</strong>
      <div class="meta">${item.count}件 / 平均予測 ${percent(item.average_probability)} / 実勝率 ${percent(item.actual_win_rate)} / gap ${percent(item.calibration_gap)} / ${item.statistical_note === "sample_small" ? "標本少" : "集計可"}</div>
    </article>
  `).join("");
}
function selectionNoteRows(notes) {
  if (!notes.length) return "";
  return `
    <article class="analysis-card"><strong>候補メモ</strong><div class="meta">購入連携や金額計算ではなく、判断材料の整理です。</div></article>
    ${notes.map((item) => `
      <article class="review-row">
        <div class="row-head">
          <strong>${item.label}${item.horse_number ? ` ${item.horse_number} ${item.horse_name}` : ""}</strong>
          <span class="meta">${riskLabel(item.risk_level)}</span>
        </div>
        ${item.horse_number ? `<div class="meta">勝率 ${percent(item.win_rate)} / 複勝圏 ${percent(item.top3_rate)} / 信頼度 ${percent(item.confidence)}</div>` : ""}
        <div>${item.note}</div>
      </article>
    `).join("")}
  `;
}
function decisionSummaryRow(summary) {
  if (!summary || !summary.label) return "";
  return `
    <article class="analysis-card">
      <div class="row-head">
        <strong>レース判定 ${summary.label}</strong>
        <span class="meta">${summary.stance ?? ""}</span>
      </div>
      <div class="metrics">
        <div class="metric">1番手差<b>${percent(summary.favorite_gap)}</b></div>
        <div class="metric">複勝3番手差<b>${percent(summary.top3_gap)}</b></div>
        <div class="metric">平均信頼度<b>${percent(summary.average_confidence)}</b></div>
      </div>
      <div class="meta">${summary.note}</div>
    </article>
  `;
}
function candidateGroupRows(groups) {
  if (!groups) return "";
  const rows = ["core", "opposition", "reserves"]
    .map((key) => candidateGroupRow(groups[key]))
    .filter(Boolean)
    .join("");
  return rows ? `<article class="analysis-card"><strong>候補セット</strong><div class="meta">買い目や金額ではなく、確認対象の分類です。</div></article>${rows}` : "";
}
function candidateGroupRow(group) {
  if (!group?.horses?.length) return "";
  return `
    <article class="review-row">
      <div class="row-head">
        <strong>${group.label}</strong>
        <span class="meta">${group.horses.length}頭</span>
      </div>
      <div class="meta">${group.note}</div>
      <div>${group.horses.map((item) => `${item.horse_number} ${item.horse_name} (${percent(item.win_rate)} / 複 ${percent(item.top3_rate)})`).join(" / ")}</div>
    </article>
  `;
}
function scenarioConsensusRows(rows) {
  if (!rows?.length) return "";
  return `
    <article class="analysis-card"><strong>展開一致度</strong><div class="meta">複数シナリオで上位3頭に残る候補です。</div></article>
    ${rows.slice(0, 6).map((item) => `
      <article class="review-row">
        <div class="row-head">
          <strong>${item.horse_number} ${item.horse_name}</strong>
          <span class="meta">${item.top3_scenario_count}/${item.scenario_count}</span>
        </div>
        <div class="meta">一致度 ${percent(item.consensus_rate)} / ${item.scenarios.join(" / ")}</div>
      </article>
    `).join("")}
  `;
}
function attentionRunRows(rows) {
  if (!rows?.length) return "";
  return `
    <article class="analysis-card"><strong>要確認解析</strong></article>
    ${rows.map((item) => `
      <article class="review-row">
        <div class="row-head">
          <strong>${reviewRaceLabel(item)}</strong>
          <span class="meta">${item.parse_status ?? "-"}</span>
        </div>
        <div class="meta">issues ${item.issue_count ?? "-"} / 特徴量充足 ${ratioLabel(item.available_feature_ratio)} / 過去走不足 ${item.missing_history_count ?? "-"}頭 / ${item.provider_name ?? "-"}</div>
        <button class="inline-button" data-learning-detail="${item.race_id}">詳細</button>
      </article>
    `).join("")}
  `;
}
function riskLabel(value) {
  return {low: "低リスク", medium: "中リスク", high: "注意"}[value] ?? value;
}
function trendLabel(value) {
  return {
    improving: "良化",
    worsening: "悪化",
    flat: "横ばい",
    insufficient: "保留"
  }[value] ?? "保留";
}
function learningInsightRows(insights) {
  if (!insights || !insights.length) return `<article class="review-row"><strong>学習メモ</strong><div class="meta">レビューが溜まると表示されます。</div></article>`;
  return `
    <article class="analysis-card"><strong>学習メモ</strong></article>
    ${insights.map((item) => `<article class="review-row">${item.message}<div class="meta">${item.statistical_note === "sample_small" ? "標本少" : item.type}</div></article>`).join("")}
  `;
}
function userPredictionCard(entry) {
  const horseNumber = Number(entry.horse_number.value);
  const saved = current.latest_user_predictions?.[horseNumber] || current.latest_user_predictions?.[String(horseNumber)] || {};
  return `
    <article class="analysis-card user-prediction" data-horse-number="${entry.horse_number.value}">
      <div class="row-head">
        <strong class="user-prediction-name">${entry.horse_number.value} ${entry.horse.horse_name.value}</strong>
        ${markSelect(saved.mark)}
      </div>
    </article>
  `;
}
function conditionLabel(dimension) {
  return {
    racecourse: "競馬場",
    surface: "馬場",
    distance_band: "距離帯",
    track_condition: "馬場状態"
  }[dimension] ?? dimension;
}
function markSelect(selected) {
  return `
    <select class="form-select mark-select" data-field="mark">
      ${["", "◎", "○", "▲", "△", "☆", "消"].map((value) => `<option${value === selected ? " selected" : ""}>${value}</option>`).join("")}
    </select>
  `;
}
function horseConditionLabel(dimension) {
  return {
    popularity_band: "人気帯",
    frame_number: "枠",
    sex: "性別",
    age_band: "年齢帯"
  }[dimension] ?? dimension;
}
function horseConditionValue(dimension, value) {
  const labels = {
    favored_1_3: "1-3番人気",
    mid_4_6: "4-6番人気",
    longshot_7_plus: "7番人気以下",
    age_3_or_less: "3歳以下",
    age_4_5: "4-5歳",
    age_6_plus: "6歳以上"
  };
  if (dimension === "frame_number") return `${value}枠`;
  return labels[value] ?? value;
}
function featureDigest(features) {
  if (!features) return "";
  const parts = [
    `枠 ${features.frame_number ?? "-"}`,
    `${features.surface ?? "-"}${features.distance ?? "-"}m`,
    `近走平均 ${features.past_5_average_finish ?? "-"}`,
    `勝ち切り ${percent(features.past_5_win_rate)}`,
    `同距離複勝 ${percent(features.same_distance_top3_rate)}`,
    `上がり3F ${features.past_5_average_final_three_furlongs ?? "-"}`
  ];
  return parts.join(" / ");
}
function limitedReasons(items) {
  return (items || []).slice(0, 5);
}
function meaningfulUserPredictions(predictions) {
  const result = {};
  Object.entries(predictions).forEach(([horseNumber, prediction]) => {
    if (Object.values(prediction).some((value) => value !== null && value !== "")) {
      result[horseNumber] = prediction;
    }
  });
  return result;
}
function escapeAttr(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}
function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}
