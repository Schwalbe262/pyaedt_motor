"use strict";

const POLL_MS = 10000;
let paused = false;
let polling = false;
let timer = null;

const byId = (id) => document.getElementById(id);
const statusKorean = {
  complete: "완료",
  running: "실행 중",
  waiting: "대기",
  ready: "준비",
  conditional: "조건부",
  skipped: "생략",
  failed: "실패",
  unavailable: "확인 필요",
};

const finite = (value) => typeof value === "number" && Number.isFinite(value);
const integer = (value, fallback = 0) => Number.isInteger(value) ? value : fallback;
const decimal = (value, digits = 1) => finite(value) ? value.toFixed(digits) : "—";

function localTime(value) {
  if (!value) return "—";
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function durationHours(value) {
  if (!finite(value)) return { value: "—", unit: "시간" };
  if (value < 24) return { value: Math.max(0, value).toFixed(value < 10 ? 1 : 0), unit: "시간" };
  return { value: (value / 24).toFixed(1), unit: "일" };
}

function setText(id, value) {
  const element = byId(id);
  if (element) element.textContent = value;
}

function empty(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderAlerts(data) {
  const container = byId("alerts");
  empty(container);
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  const errors = Array.isArray(data.errors) ? data.errors : [];
  [...alerts, ...errors.map((item) => ({ level: "error", message: item.message }))]
    .slice(0, 4)
    .forEach((item) => {
      const alert = element("div", `alert ${item.level || "info"}`, item.message || "상태 확인 필요");
      alert.setAttribute("role", item.level === "error" ? "alert" : "status");
      container.appendChild(alert);
    });
}

function renderCampaign(data) {
  const campaign = data.campaign || {};
  const scheduler = data.scheduler || {};
  const total = integer(campaign.total, 700);
  const result = integer(campaign.result_ok);
  const active = scheduler.reachable ? integer(scheduler.active_count) : integer(campaign.active);
  const cap = integer(scheduler.cap, integer(campaign.cap, 100));
  const schedulerCounts = scheduler.status_counts || {};
  const running = integer(schedulerCounts.running);
  const assigning = integer(schedulerCounts.queued) + integer(schedulerCounts.attaching);
  const progress = finite(campaign.progress_pct) ? campaign.progress_pct : 0;
  const heroProgress = byId("heroProgress");
  heroProgress.max = total;
  heroProgress.value = Math.min(result, total);
  setText("heroProgressLabel", `${result.toLocaleString("ko-KR")} / ${total.toLocaleString("ko-KR")}`);
  setText("heroPercent", `${progress.toFixed(1)}%`);
  setText("resultOk", result.toLocaleString("ko-KR"));
  setText("resultTotal", `/ ${total.toLocaleString("ko-KR")}`);
  setText("resultSub", `scheduler 완료 ${integer(campaign.scheduler_ok)}건 · 안정화 ${integer(campaign.settling_results)}건`);
  setText("activeSlots", active.toLocaleString("ko-KR"));
  setText("activeCap", `/ ${cap}`);
  setText("slotSub", `실행 ${running} · 배정/대기 ${assigning}`);
  setText("completionRate", decimal(campaign.completion_rate_per_hour, 1));
  setText("rateSub", `최근 최대 6시간 · 제출 ${integer(campaign.submitted)}건`);
  const eta = durationHours(campaign.eta_hours);
  setText("etaValue", eta.value);
  setText("etaUnit", eta.unit);
  setText("etaSub", finite(campaign.eta_hours) ? "현재 처리 속도 기준 추정" : "완료 표본이 더 필요합니다");
  const supervisorAlive = Array.isArray(data.processes)
    && data.processes.some((item) => item.role === "supervisor" && item.state === "alive");
  setText("heroDescription", active >= cap && integer(campaign.retry) === 0
    ? `${cap}개 슬롯 점유 · 실제 실행 ${running}건 · 배정/대기 ${assigning}건입니다.`
    : supervisorAlive
      ? `활성 ${active}건 · supervisor가 완료 결과를 검증한 뒤 빈 슬롯을 다시 채우는 중입니다.`
      : `활성 ${active}건 · 결과 검증 ${result}건 · 재시도 ${integer(campaign.retry)}건`);
}

function renderPipeline(data) {
  const pipeline = data.pipeline || {};
  const stages = Array.isArray(pipeline.stages) ? pipeline.stages : [];
  setText("currentStageChip", pipeline.current_label || "상태 확인 필요");
  setText("heroTitle", data.health === "running"
    ? `${pipeline.current_label || "Stage 1"} · ${data.headline || "실행 중"}`
    : pipeline.current_label || data.headline || "진행 상태 확인 필요");
  const list = byId("pipelineList");
  empty(list);
  stages.forEach((stage, index) => {
    const item = element("li", `pipeline-item ${stage.status || "waiting"}`);
    item.appendChild(element("span", "stage-node", stage.status === "complete" ? "✓" : String(index + 1).padStart(2, "0")));
    const copy = element("div", "stage-copy");
    copy.appendChild(element("strong", "", stage.label || "—"));
    copy.appendChild(element("small", "", stage.detail || ""));
    item.appendChild(copy);
    item.appendChild(element("span", "stage-status", statusKorean[stage.status] || stage.status || "대기"));
    list.appendChild(item);
  });
}

function renderScheduler(data) {
  const scheduler = data.scheduler || {};
  const counts = scheduler.status_counts || {};
  const health = byId("schedulerHealth");
  if (scheduler.reachable && !scheduler.stale) {
    health.textContent = "정상 연결";
    health.className = "health-pill complete";
  } else if (scheduler.stale) {
    health.textContent = "마지막 정상 값";
    health.className = "health-pill warning";
  } else {
    health.textContent = "연결 안 됨";
    health.className = "health-pill failed";
  }
  setText("schedulerRunning", integer(counts.running));
  setText("schedulerCompleted", integer(counts.completed));
  setText("schedulerFailed", integer(counts.failed));
  setText("lastHour", integer(scheduler.completed_last_hour));
  setText("taskTotal", `프로젝트 이력 ${integer(scheduler.project_total_count).toLocaleString("ko-KR")}건`);
  const nodes = Array.isArray(scheduler.nodes) ? scheduler.nodes : [];
  setText("nodeCount", `${nodes.length} nodes`);
  const nodeGrid = byId("nodeGrid");
  empty(nodeGrid);
  nodes.forEach((node) => {
    const card = element("article", "node-card");
    const header = document.createElement("header");
    header.appendChild(element("strong", "", node.node || "unknown"));
    header.appendChild(element("span", "", `${integer(node.active_tasks)} tasks`));
    card.appendChild(header);
    const bar = document.createElement("progress");
    bar.className = "node-bar";
    bar.max = 20;
    bar.value = Math.max(0, Math.min(20, integer(node.active_tasks)));
    bar.setAttribute("aria-label", `${node.node || "노드"} 활성 작업 수`);
    card.appendChild(bar);
    const cpu = finite(node.cpu_load_pct) ? `CPU ${decimal(node.cpu_load_pct)}%` : `${integer(node.requested_cpus)} CPU 요청`;
    card.appendChild(element("small", "", `${cpu} · ${integer(node.allocation_count)} allocation`));
    nodeGrid.appendChild(card);
  });
  if (!nodes.length) nodeGrid.appendChild(element("p", "muted", "활성 노드 정보가 없습니다."));
}

function renderModel(data) {
  const model = data.model || {};
  setText("r2Threshold", finite(model.threshold) ? model.threshold.toFixed(2) : "0.95");
  setText("r2Passed", integer(model.passed_count));
  const stateText = {
    passed: "모든 지표가 품질 목표를 통과했습니다",
    failed: "일부 지표가 R² 목표에 미달했습니다",
    unavailable: "모델 품질 산출물을 확인할 수 없습니다",
    waiting: "Stage 1 완료 후 학습됩니다",
  }[model.gate_status] || "Surrogate 학습 상태 확인 중";
  setText("modelState", stateText);
  setText("modelStats", model.available
    ? `${model.stage || "현재"} · 최소 R² ${decimal(model.min_r2, 4)} · 평균 R² ${decimal(model.avg_r2, 4)}`
    : "독립 test split에서 8개 primary + 전압 1개를 평가합니다.");
  const list = byId("metricList");
  empty(list);
  const metrics = Array.isArray(model.metrics) ? model.metrics : [];
  metrics.forEach((metric) => {
    const row = element("div", `metric-row ${metric.r2 === null ? "pending" : metric.passed ? "pass" : "fail"}`);
    row.appendChild(element("span", "", metric.label || metric.target || "—"));
    const progress = document.createElement("progress");
    progress.max = 1;
    progress.value = finite(metric.r2) ? Math.max(0, Math.min(1, metric.r2)) : 0;
    progress.setAttribute("aria-label", `${metric.label || "지표"} R²`);
    row.appendChild(progress);
    row.appendChild(element("strong", "", finite(metric.r2) ? metric.r2.toFixed(4) : "대기"));
    list.appendChild(row);
  });
}

function renderPhysics(data) {
  const beta = data.beta || {};
  const optimization = data.optimization || {};
  const gate = byId("betaGate");
  gate.textContent = beta.passed ? "물리 gate 통과" : beta.available ? "gate 실패" : "확인 필요";
  gate.className = `health-pill ${beta.passed ? "complete" : beta.available ? "failed" : "warning"}`;
  setText("electricalZero", finite(beta.electrical_zero_deg) ? `${beta.electrical_zero_deg.toFixed(3)}°` : "—");
  setText("bestBeta", finite(beta.best_beta_deg) ? `${beta.best_beta_deg.toFixed(2)}°` : "—");
  setText("bestTorque", finite(beta.best_torque_nm) ? `${beta.best_torque_nm.toFixed(3)} N·m` : "—");
  setText("dqError", finite(beta.dq_relative_error) ? beta.dq_relative_error.toExponential(2) : "—");
  setText("targetTorque", finite(optimization.target_torque_nm) ? `${optimization.target_torque_nm.toFixed(1)} N·m` : "—");
  setText("targetTorqueSpeed", `@ ${integer(optimization.target_torque_speed_rpm).toLocaleString("ko-KR")} rpm`);
  setText("targetPower", finite(optimization.target_power_kw) ? `${optimization.target_power_kw.toFixed(1)} kW` : "—");
  setText("targetPowerSpeed", `@ ${integer(optimization.target_power_speed_rpm).toLocaleString("ko-KR")} rpm`);
  setText("constraintNote", optimization.spec_status === "verified"
    ? `독립 운전점 제약 · 65.1 N·m @ 1,200 rpm = ${decimal(optimization.torque_point_power_kw, 3)} kW · 7.5 kW @ 5,000 rpm = ${decimal(optimization.power_point_torque_nm, 3)} N·m`
    : "최적화 spec 확인 실패 · 화면에는 기본 목표값이 표시됩니다.");
  const nsga = byId("nsgaProgress");
  empty(nsga);
  const seedProgress = Array.isArray(optimization.seeds) ? optimization.seeds : [];
  const configuredSeeds = Array.isArray(optimization.configured_seeds) ? optimization.configured_seeds : [42, 43, 44];
  const progressBySeed = new Map(seedProgress.map((item) => [integer(item.seed, -1), item]));
  if (seedProgress.length) {
    configuredSeeds.forEach((configuredSeed) => {
      const seed = progressBySeed.get(integer(configuredSeed)) || { seed: configuredSeed };
      const row = element("div", "seed-row");
      row.appendChild(element("span", "", `seed ${integer(seed.seed)}`));
      const progress = document.createElement("progress");
      progress.max = Math.max(1, integer(seed.max_generations, integer(optimization.max_generations, 300)));
      progress.value = Math.min(progress.max, integer(seed.completed_generations));
      progress.setAttribute("aria-label", `NSGA-II seed ${integer(seed.seed)} generation 진행률`);
      row.appendChild(progress);
      row.appendChild(element("strong", "", progressBySeed.has(integer(configuredSeed))
        ? `${integer(seed.completed_generations)} / ${integer(seed.max_generations)} · ${integer(seed.n_eval)} eval`
        : "대기"));
      nsga.appendChild(row);
    });
  } else {
    nsga.appendChild(element("p", "", `NSGA-II 대기 · ${configuredSeeds.length} seeds · population ${integer(optimization.population_size, 160)} · ${integer(optimization.max_generations, 300)} generations`));
  }
  if (integer(optimization.pareto_candidates) || integer(optimization.fea_case_rows)) {
    nsga.appendChild(element("p", "", `Pareto ${integer(optimization.pareto_candidates)}개 · FEA 검증 행 ${integer(optimization.fea_case_rows)}개`));
  }
}

function renderProcesses(data) {
  const processes = Array.isArray(data.processes) ? data.processes : [];
  const grid = byId("processGrid");
  empty(grid);
  processes.forEach((process) => {
    const card = element("article", `process-card ${process.state || "unknown"}`);
    card.appendChild(element("i", "process-dot"));
    const copy = document.createElement("div");
    copy.appendChild(element("strong", "", process.label || process.role || "watcher"));
    const activity = process.activity === "running"
      ? "실행 중"
      : process.activity === "waiting"
        ? "armed · 대기"
        : process.activity === "managed_by_supervisor"
          ? "supervisor가 단계별 실행"
          : process.state === "stopped" ? "중지" : "확인 필요";
    copy.appendChild(element("small", "", activity));
    card.appendChild(copy);
    grid.appendChild(card);
  });

  const speed = data.speed || {};
  const speedCounts = speed.scheduler_counts || {};
  const expected = integer(speed.expected_rows, 24);
  const verified = speed.complete ? expected : Math.min(expected, integer(speed.result_rows));
  const schedulerCompleted = Math.min(expected, integer(speedCounts.completed));
  const progressValue = Math.max(verified, schedulerCompleted);
  const active = integer(speedCounts.running) + integer(speedCounts.queued) + integer(speedCounts.attaching);
  const progress = byId("speedProgress");
  progress.max = expected;
  progress.value = progressValue;
  setText("speedRows", speed.complete
    ? `${verified} / ${expected} 검증 완료`
    : `${verified} 검증 · ${schedulerCompleted} / ${expected} scheduler 완료`);
  setText("speedStatus", speed.complete
    ? "완료"
    : active
      ? `${active}건 실행/배정`
      : schedulerCompleted
        ? `${schedulerCompleted}건 완료 · 결과 검증 중`
        : speed.plan_rows ? "계획 완료 · 제출 대기" : "cap-직렬 대기");
}

function renderTasks(data) {
  const tasks = Array.isArray(data.scheduler?.recent_tasks) ? data.scheduler.recent_tasks : [];
  const body = byId("taskRows");
  empty(body);
  tasks.forEach((task) => {
    const row = document.createElement("tr");
    const cleanName = (task.name || "—")
      .replace("ipmsm-v2-foundation-s1-", "S1 · ")
      .replace("ipmsm-v2-foundation-s2-", "S2 · ")
      .replace("ipmsm-v2-foundation-s3-", "S3 · ")
      .replace("ipmsm-v2-pareto-fea-", "Pareto · ");
    row.appendChild(element("td", "", cleanName));
    row.appendChild(element("td", "", task.node || "—"));
    const statusCell = document.createElement("td");
    statusCell.appendChild(element("span", `task-status ${task.status || "unknown"}`, task.status || "unknown"));
    row.appendChild(statusCell);
    row.appendChild(element("td", "", localTime(task.finished_at || task.started_at)));
    body.appendChild(row);
  });
  if (!tasks.length) {
    const row = document.createElement("tr");
    const cell = element("td", "muted", "최근 작업 정보가 없습니다.");
    cell.colSpan = 4;
    row.appendChild(cell);
    body.appendChild(row);
  }
}

function render(data) {
  setText("projectName", data.project || "PYAEDT_MOTOR_IPMSM_V2");
  const observed = data.campaign?.status_observed_at || data.scheduler?.updated_at || data.generated_at;
  setText("updatedAt", `상태 관측 ${localTime(observed)} · API ${localTime(data.generated_at)}`);
  setText("footerSchema", data.schema_version || "IPMSM dashboard");
  const liveBadge = byId("liveBadge");
  liveBadge.classList.toggle("offline", data.health === "degraded" && !data.scheduler?.reachable);
  liveBadge.classList.toggle("stale", data.health === "degraded" && Boolean(data.scheduler?.reachable));
  setText("liveLabel", data.health === "degraded" ? (data.scheduler?.reachable ? "STALE" : "OFFLINE") : "LIVE");
  renderCampaign(data);
  renderPipeline(data);
  renderAlerts(data);
  renderScheduler(data);
  renderModel(data);
  renderPhysics(data);
  renderProcesses(data);
  renderTasks(data);
}

async function refresh() {
  if (polling) return;
  polling = true;
  byId("refreshButton").disabled = true;
  try {
    const response = await fetch("/api/status", { cache: "no-store", headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    byId("liveBadge").classList.add("offline");
    byId("liveBadge").classList.remove("stale");
    setText("liveLabel", "OFFLINE");
    setText("updatedAt", "대시보드 연결 재시도 중");
    const container = byId("alerts");
    empty(container);
    container.appendChild(element("div", "alert error", "대시보드 상태 API에 연결할 수 없습니다. 자동으로 다시 시도합니다."));
  } finally {
    polling = false;
    byId("refreshButton").disabled = false;
  }
}

function schedule() {
  clearInterval(timer);
  if (!paused) timer = setInterval(refresh, POLL_MS);
}

byId("refreshButton").addEventListener("click", refresh);
byId("pauseButton").addEventListener("click", () => {
  paused = !paused;
  byId("pauseButton").setAttribute("aria-pressed", String(paused));
  byId("pauseButton").textContent = paused ? "자동 갱신 다시 시작" : "자동 갱신 일시정지";
  schedule();
});

refresh();
schedule();
