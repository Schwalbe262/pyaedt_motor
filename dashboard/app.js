"use strict";

const POLL_MS = 10000;
const FETCH_TIMEOUT_MS = 8000;
const SNAPSHOT_STALE_MS = 30000;
const REFRESH_LABEL = "최신 스냅샷 다시 불러오기";
const REFRESH_LOADING_LABEL = "최신 스냅샷 불러오는 중…";
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
const provisionalLabels = {
  output_coreloss_last_avg_w: "철손",
  output_efficiency_last_pct: "효율",
  output_ld_last_avg_h: "Ld",
  output_lq_last_avg_h: "Lq",
  output_solidloss_last_avg_w: "와전류손",
  output_torque_last_avg_nm: "평균 토크",
  output_torque_last_max_nm: "최대 토크",
  output_total_loss_last_avg_w: "총손실",
};
const runtimeUnitLabels = {
  physics_gate: "물리 gate",
  validated_results: "검증 결과",
  r2_targets_passed: "R² 통과 지표",
  result_rows: "결과",
  nsga_seed_generations: "세대",
  pareto_fea_tasks: "FEA 작업",
  validated_rows: "검증 결과",
  matched_probes: "매칭 probe",
};

const finite = (value) => typeof value === "number" && Number.isFinite(value);
const integer = (value, fallback = 0) => Number.isInteger(value) ? value : fallback;
const count = (value) => Number.isInteger(value) && value >= 0 ? value : null;
const decimal = (value, digits = 1) => finite(value) ? value.toFixed(digits) : "—";
const record = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};

function stageRuntime(stage) {
  const runtime = stage && typeof stage.runtime === "object" && stage.runtime !== null
    ? stage.runtime
    : {};
  const completed = count(runtime.completed);
  const total = count(runtime.total);
  const suppliedProgress = finite(runtime.progress_pct) ? runtime.progress_pct : null;
  const rawUnit = typeof runtime.unit === "string" ? runtime.unit.trim() : "";
  const rawRunner = record(runtime.runner_progress);
  const runnerProgress = {
    available: rawRunner.available === true,
    resultOk: count(rawRunner.result_ok),
    auditPending: count(rawRunner.audit_pending),
    submitted: count(rawRunner.submitted),
    active: count(rawRunner.active),
    missing: count(rawRunner.missing),
    retry: count(rawRunner.retry),
    schedulerOk: count(rawRunner.scheduler_ok),
    total: count(rawRunner.total),
  };
  const progressPct = suppliedProgress !== null
    ? Math.max(0, Math.min(100, suppliedProgress))
    : completed !== null && total !== null && total > 0
      ? Math.max(0, Math.min(100, 100 * completed / total))
      : null;
  return {
    completed,
    total,
    rawUnit,
    unit: runtimeUnitLabels[rawUnit] || rawUnit,
    progressPct,
    planned: count(runtime.planned),
    schedulerCounts: runtime.scheduler_counts && typeof runtime.scheduler_counts === "object"
      ? runtime.scheduler_counts
      : {},
    runnerProgress,
  };
}

function runtimeCounter(runtime, includePercent = true) {
  const parts = [];
  if (runtime.completed !== null && runtime.total !== null) {
    parts.push(`${runtime.completed.toLocaleString("ko-KR")} / ${runtime.total.toLocaleString("ko-KR")}${runtime.unit ? ` ${runtime.unit}` : ""}`);
  } else if (runtime.completed !== null) {
    parts.push(`${runtime.completed.toLocaleString("ko-KR")}${runtime.unit ? ` ${runtime.unit}` : ""}`);
  }
  if (includePercent && runtime.progressPct !== null) parts.push(`${runtime.progressPct.toFixed(1)}%`);
  if (runtime.planned !== null && (runtime.total === null || runtime.planned !== runtime.total)) {
    parts.push(runtime.rawUnit === "nsga_seed_generations"
      ? `계획 seed ${runtime.planned.toLocaleString("ko-KR")}개`
      : `계획 ${runtime.planned.toLocaleString("ko-KR")}${runtime.unit ? ` ${runtime.unit}` : ""}`);
  }
  return parts.join(" · ");
}

function currentProgressRuntime(currentId, runtime) {
  const runner = runtime.runnerProgress || {};
  const runnerAhead = ["stage2", "stage3"].includes(currentId)
    && runner.available === true
    && runner.resultOk !== null
    && runner.total !== null
    && runner.total > 0
    && runner.resultOk <= runner.total
    && (runtime.completed === null || runner.resultOk > runtime.completed);
  if (!runnerAhead) return { ...runtime, progressSource: "final_collection" };
  return {
    ...runtime,
    completed: runner.resultOk,
    total: runner.total,
    rawUnit: "live_runner_results",
    unit: "live runner 검증",
    progressPct: Math.max(0, Math.min(100, 100 * runner.resultOk / runner.total)),
    progressSource: "live_runner",
  };
}

function schedulerComposition(rawCounts) {
  const counts = rawCounts && typeof rawCounts === "object" ? rawCounts : {};
  const active = integer(counts.queued) + integer(counts.attaching) + integer(counts.running);
  const completed = integer(counts.completed);
  const failed = integer(counts.failed) + integer(counts.cancelled);
  return { active, completed, failed };
}

function effectivePipelineState(data, rawStages, requestedCurrentId) {
  const context = governanceContext(data);
  if (!context.active) return { stages: rawStages, blocked: false, forcedCurrentId: "" };

  const campaign = data.campaign || {};
  const stage1Incomplete = integer(campaign.result_ok) < Math.max(1, integer(campaign.total, 700));
  const quality = qualityGateState(data);
  const authorization = authorizationGateState(data);
  const blocked = stage1Incomplete || !quality.passed || !authorization.approved;
  if (!blocked) return { stages: rawStages, blocked: false, forcedCurrentId: "" };

  const optimizationIndex = rawStages.findIndex((stage) => stage.id === "optimization");
  if (optimizationIndex < 0) return { stages: rawStages, blocked: true, forcedCurrentId: "" };
  const rawOptimization = rawStages[optimizationIndex] || {};
  const executionContradiction = ["running", "complete"].includes(rawOptimization.status);
  const optimizationFailed = quality.failed
    || authorization.rejected
    || executionContradiction
    || ["failed", "unavailable"].includes(rawOptimization.status);
  const stages = rawStages.map((stage, index) => {
    if (index < optimizationIndex) return stage;
    const runtime = record(stage.runtime);
    if (index === optimizationIndex) {
      return {
        ...stage,
        status: optimizationFailed ? "failed" : "waiting",
        detail: "v4 공식 R² authority와 authorization gate 충족 전 실행 차단",
        runtime: { ...runtime, completed: 0, progress_pct: 0 },
      };
    }
    return {
      ...stage,
      status: "waiting",
      detail: "최적화 governance gate가 유효해진 뒤 실행",
      runtime: { ...runtime, completed: 0, progress_pct: 0 },
    };
  });
  const requestedIndex = rawStages.findIndex((stage) => stage.id === requestedCurrentId);
  return {
    stages,
    blocked: true,
    forcedCurrentId: requestedIndex >= optimizationIndex || (requestedIndex < 0 && executionContradiction)
      ? "optimization"
      : "",
  };
}

function overallState(data) {
  const pipeline = data.pipeline || {};
  const rawStages = Array.isArray(pipeline.stages) ? pipeline.stages : [];
  const overall = data.overall && typeof data.overall === "object" ? data.overall : {};
  const requestedCurrentId = overall.current_stage || pipeline.current_stage || "";
  const effectivePipeline = effectivePipelineState(data, rawStages, requestedCurrentId);
  const stages = effectivePipeline.stages;
  const currentId = effectivePipeline.forcedCurrentId || requestedCurrentId;
  const currentStage = stages.find((stage) => stage.id === currentId)
    || stages.find((stage) => stage.status === "running")
    || stages.find((stage) => stage.status === "ready")
    || stages.find((stage) => !["complete", "skipped"].includes(stage.status))
    || stages[stages.length - 1]
    || {};
  const exactRuntime = {
    runtime: {
      completed: overall.completed,
      total: overall.total,
      unit: overall.unit,
      progress_pct: overall.progress_pct,
      planned: currentStage.runtime?.planned,
      scheduler_counts: currentStage.runtime?.scheduler_counts,
      runner_progress: currentStage.runtime?.runner_progress,
    },
  };
  const hasExactRuntime = !effectivePipeline.forcedCurrentId && (
    count(overall.completed) !== null
      || count(overall.total) !== null
      || finite(overall.progress_pct)
  );
  let runtime = hasExactRuntime ? stageRuntime(exactRuntime) : stageRuntime(currentStage);
  const resolved = effectivePipeline.blocked
    ? stages.filter((stage) => ["complete", "skipped"].includes(stage.status)).length
    : count(overall.resolved_stages)
      ?? stages.filter((stage) => ["complete", "skipped"].includes(stage.status)).length;
  const totalStages = count(overall.total_stages) ?? stages.length;

  // Current v1 snapshots predate stage runtime counters. Keep their Stage 1
  // result counter as a clearly scoped fallback, never as whole-project progress.
  if (!hasExactRuntime && !runtimeCounter(runtime) && currentStage.id === "stage1") {
    const campaign = data.campaign || {};
    const completed = count(campaign.result_ok);
    const total = count(campaign.total);
    runtime = stageRuntime({
      runtime: {
        completed,
        total,
        unit: "검증 결과",
        progress_pct: finite(campaign.progress_pct) ? campaign.progress_pct : undefined,
        scheduler_counts: currentStage.runtime?.scheduler_counts,
      },
    });
  }

  const currentIndex = stages.indexOf(currentStage);
  const nextStage = (!effectivePipeline.forcedCurrentId
    ? stages.find((stage) => stage.id === overall.next_stage)
    : null)
    || stages.slice(Math.max(0, currentIndex + 1)).find((stage) => !["complete", "skipped"].includes(stage.status))
    || null;
  return {
    stages,
    currentStage,
    currentId: effectivePipeline.forcedCurrentId || overall.current_stage || currentStage.id || "unknown",
    currentLabel: effectivePipeline.forcedCurrentId
      ? currentStage.label || "NSGA-II + Pareto FEA"
      : overall.current_label || currentStage.label || pipeline.current_label || "현재 단계 확인 필요",
    currentStatus: effectivePipeline.forcedCurrentId
      ? currentStage.status || "waiting"
      : overall.current_status || currentStage.status || "waiting",
    currentDetail: currentStage.detail || "현재 단계 산출물을 확인합니다.",
    runtime,
    progressRuntime: currentProgressRuntime(
      effectivePipeline.forcedCurrentId || overall.current_stage || currentStage.id || "unknown",
      runtime,
    ),
    resolved,
    totalStages,
    nextStageId: effectivePipeline.forcedCurrentId ? nextStage?.id || "" : overall.next_stage || nextStage?.id || "",
    nextLabel: effectivePipeline.forcedCurrentId ? nextStage?.label || "계획된 다음 단계 없음" : overall.next_label || nextStage?.label || "계획된 다음 단계 없음",
    nextDetail: effectivePipeline.forcedCurrentId ? nextStage?.detail || "현재 단계의 최종 검증을 기다립니다." : overall.next_detail || nextStage?.detail || "현재 단계의 최종 검증을 기다립니다.",
  };
}

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

function estimatedFinish(hours) {
  if (!finite(hours)) return "—";
  const date = new Date(Date.now() + Math.max(0, hours) * 60 * 60 * 1000);
  return new Intl.DateTimeFormat("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function timestampAgeMs(value) {
  if (!value) return Infinity;
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
  const timestamp = new Date(normalized).getTime();
  return Number.isFinite(timestamp) ? Math.max(0, Date.now() - timestamp) : Infinity;
}

function progressAge(value) {
  if (!finite(value)) return "확인 대기";
  if (value < 60) return `${Math.round(value)}초 전`;
  if (value < 3600) return `${Math.round(value / 60)}분 전`;
  return `${(value / 3600).toFixed(1)}시간 전`;
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
  const combined = [...errors.map((item) => ({ level: "error", message: item.message })), ...alerts];
  combined.slice(0, 4).forEach((item) => {
    const alert = element("div", `alert ${item.level || "info"}`, item.message || "상태 확인 필요");
    alert.setAttribute("role", item.level === "error" ? "alert" : "status");
    container.appendChild(alert);
  });
  const overflow = Math.max(0, combined.length - 4);
  if (overflow) {
    const summary = element("div", "alert overflow", `추가 알림 ${overflow}건 · 아래 단계별 카드에서 상세 상태를 확인하세요.`);
    summary.setAttribute("role", "status");
    container.appendChild(summary);
  }
}

function blockerState(data, state) {
  const targetFailure = data.target_load?.failure;
  if (targetFailure && (targetFailure.code || targetFailure.message)) {
    return {
      summary: `Target-load 실패${targetFailure.code ? ` · ${targetFailure.code}` : ""}`,
      detail: targetFailure.message || "Target-load 실패 증거를 확인해야 합니다.",
    };
  }
  const errors = Array.isArray(data.errors) ? data.errors : [];
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  const typedError = errors[0] || alerts.find((item) => item.level === "error");
  if (typedError) {
    return { summary: `${state.currentLabel} 확인 필요`, detail: typedError.message || "상태 무결성을 확인해야 합니다." };
  }
  if (["failed", "unavailable"].includes(state.currentStatus)) {
    return { summary: `${state.currentLabel} ${statusKorean[state.currentStatus]}`, detail: state.currentDetail };
  }
  const optimizationStage = state.stages.find((stage) => stage.id === "optimization");
  const authorization = authorizationGateState(data);
  const authorizationPending = authorization.v4Active
    ? !authorization.approved
    : data.optimization?.requires_user_confirmation === true && !authorization.approved;
  if (
    authorizationPending
    && (authorization.v4Active || !["complete", "skipped"].includes(optimizationStage?.status))
  ) {
    return {
      summary: authorization.confirmed ? "최적화 authorization 대기" : "향후 최적화 입력 확인",
      detail: authorization.confirmed
        ? "입력 확인은 완료됐지만 검증된 authorization receipt 전에는 Production NSGA-II를 시작하지 않습니다."
        : "Production NSGA-II 전 운전점 수치·duty·권선 가정을 확인해야 합니다.",
    };
  }
  return {
    summary: "현재 확인된 차단 없음",
    detail: state.nextStageId
      ? `${state.currentLabel} 검증 후 ${state.nextLabel}(으)로 진행합니다.`
      : "남은 산출물의 최종 무결성을 확인합니다.",
  };
}

function readinessState(id, state) {
  const node = byId(id);
  if (node) node.dataset.state = state;
}

function inputApprovalState(optimization) {
  const confirmationStatus = String(
    optimization.confirmation_status || optimization.input_confirmation_status || "",
  ).trim().toLowerCase();
  const authorizationStatus = String(optimization.authorization_status || "").trim().toLowerCase();
  const authorizedTokens = new Set(["approved", "authorized", "complete", "valid"]);
  const confirmedTokens = new Set(["approved", "confirmed", "complete", "valid"]);
  const rejectedTokens = new Set(["failed", "invalid", "rejected", "revoked"]);
  return {
    approved: optimization.requires_user_confirmation === false || authorizedTokens.has(authorizationStatus),
    confirmed: confirmedTokens.has(confirmationStatus),
    rejected: rejectedTokens.has(confirmationStatus) || rejectedTokens.has(authorizationStatus),
    confirmationStatus,
    authorizationStatus,
  };
}

function governanceContext(data) {
  const governance = record(data.governance);
  const contract = record(governance.contract);
  return {
    active: contract.activated === true,
    governance,
    contract,
    officialStage1: record(governance.official_stage1),
    confirmation: record(governance.confirmation),
    authorization: record(governance.authorization),
  };
}

function isDiagnosticPreview(model) {
  const value = record(model);
  return value.diagnostic_only === true
    && value.authority_verified === false
    && value.official_gate_eligible === false;
}

function qualityGateState(data) {
  const context = governanceContext(data);
  if (!context.active) {
    const model = record(data.model);
    const diagnosticOnly = isDiagnosticPreview(model);
    const authorityVerified = Boolean(model.available) && model.authority_verified !== false;
    const officialGateEligible = authorityVerified && model.official_gate_eligible !== false;
    return {
      v4Active: false,
      diagnosticOnly,
      authorityVerified,
      officialGateEligible,
      available: Boolean(model.available) && officialGateEligible,
      passed: officialGateEligible && model.gate_status === "passed",
      failed: !diagnosticOnly && ["failed", "unavailable"].includes(model.gate_status),
      gateStatus: model.gate_status || "waiting",
      threshold: finite(model.threshold) ? model.threshold : 0.95,
      passedCount: integer(model.passed_count),
      targetCount: Math.max(9, Array.isArray(model.metrics) ? model.metrics.length : 0),
      minR2: model.min_r2,
      avgR2: model.avg_r2,
    };
  }

  const official = context.officialStage1;
  const gateStatus = String(official.gate_status || "waiting").trim().toLowerCase();
  const invalidTokens = new Set(["failed", "invalid", "artifact_invalid", "rejected", "revoked"]);
  const contractInvalid = invalidTokens.has(String(context.contract.status || "").toLowerCase());
  const authorityVerified = !contractInvalid
    && official.completion_present === true
    && official.status === "verified"
    && official.r2_authority === "verified";
  return {
    v4Active: true,
    diagnosticOnly: isDiagnosticPreview(data.model),
    authorityVerified,
    officialGateEligible: authorityVerified,
    available: authorityVerified && ["passed", "failed"].includes(gateStatus),
    passed: authorityVerified && gateStatus === "passed",
    failed: contractInvalid
      || invalidTokens.has(String(official.status || "").toLowerCase())
      || invalidTokens.has(String(official.r2_authority || "").toLowerCase())
      || (authorityVerified && gateStatus === "failed"),
    gateStatus,
    threshold: finite(official.threshold) ? official.threshold : 0.95,
    passedCount: integer(official.passed_count),
    targetCount: Math.max(1, integer(official.target_count, 9)),
    minR2: official.min_r2,
    avgR2: official.avg_r2,
  };
}

function authorizationGateState(data) {
  const context = governanceContext(data);
  if (!context.active) {
    const input = inputApprovalState(data.optimization || {});
    if (isDiagnosticPreview(data.model)) {
      return {
        ...input,
        v4Active: false,
        approved: false,
        diagnosticBlocked: true,
      };
    }
    return { ...input, v4Active: false, diagnosticBlocked: false };
  }

  const confirmationStatus = String(context.confirmation.status || "").trim().toLowerCase();
  const authorizationStatus = String(context.authorization.status || "").trim().toLowerCase();
  const invalidTokens = new Set(["failed", "invalid", "artifact_invalid", "rejected", "revoked"]);
  const confirmed = context.confirmation.confirmation_present === true
    && context.confirmation.confirmed === true
    && ["verified", "confirmed"].includes(confirmationStatus);
  const receiptPresent = context.authorization.receipt_present === true;
  const rejected = invalidTokens.has(confirmationStatus)
    || invalidTokens.has(authorizationStatus)
    || invalidTokens.has(String(context.governance.status || "").toLowerCase())
    || invalidTokens.has(String(context.contract.status || "").toLowerCase());
  return {
    v4Active: true,
    diagnosticBlocked: false,
    approved: !rejected
      && confirmed
      && receiptPresent
      && authorizationStatus === "authorized"
      && context.authorization.authorized === true
      && context.governance.status === "authorized"
      && context.contract.status === "verified",
    confirmed,
    rejected,
    confirmationStatus,
    authorizationStatus,
    declarationStatus: String(context.confirmation.declaration_status || "").trim().toLowerCase(),
    receiptPresent,
  };
}

function renderHeroOperations(data, state) {
  const progressRuntime = state.progressRuntime || state.runtime;
  const totalStages = Math.max(1, state.totalStages || state.stages.length || 1);
  const currentResolved = ["complete", "skipped"].includes(state.currentStatus);
  const currentFraction = !currentResolved && progressRuntime.progressPct !== null
    ? Math.max(0, Math.min(1, progressRuntime.progressPct / 100))
    : 0;
  const stageUnits = Math.min(totalStages, state.resolved + currentFraction);
  const overallPct = 100 * stageUnits / totalStages;
  const overallProgress = byId("overallProgress");
  overallProgress.max = 100;
  overallProgress.value = overallPct;
  setText("overallPercent", `${overallPct.toFixed(1)}%`);
  setText(
    "overallProgressNote",
    `${state.resolved}/${totalStages}단계 해결 · 현재 ${state.currentLabel} ${progressRuntime.progressSource === "live_runner" ? "live runner 검증 " : ""}${progressRuntime.progressPct === null ? "수치 대기" : `${progressRuntime.progressPct.toFixed(1)}%`} · 단계별 균등 가중`,
  );

  const campaign = data.campaign || {};
  const scheduler = data.scheduler || {};
  const total = Math.max(1, integer(campaign.total, 700));
  const result = Math.max(0, integer(campaign.result_ok));
  const active = scheduler.reachable ? integer(scheduler.active_count) : integer(campaign.active);
  const cap = Math.max(1, integer(scheduler.cap, integer(campaign.cap, 100)));
  const collectionVerified = campaign.completion_source === "atomic_collection"
    && campaign.collection_integrity_status === "verified";
  const snapshotFresh = timestampAgeMs(data.generated_at) <= SNAPSHOT_STALE_MS;
  const topLevelFresh = data.stale !== true
    && ["running", "healthy", "ok", "idle"].includes(String(data.health || "").trim().toLowerCase());
  const collectionFresh = collectionVerified && snapshotFresh && topLevelFresh;
  const schedulerFresh = scheduler.reachable === true
    && scheduler.stale !== true
    && snapshotFresh
    && topLevelFresh;
  const stage1Pct = Math.max(0, Math.min(100, 100 * result / total));
  setText("stage1ReadinessValue", `${result.toLocaleString("ko-KR")} / ${total.toLocaleString("ko-KR")}`);
  setText(
    "stage1ReadinessDetail",
    collectionVerified
      ? collectionFresh
        ? `Atomic collection ${integer(campaign.collection_result_files)}건 무결성 검증 완료`
        : `마지막 검증: atomic collection ${integer(campaign.collection_result_files)}건 · snapshot 갱신 필요`
      : schedulerFresh
      ? `${stage1Pct.toFixed(1)}% · active ${active} / cap ${cap}`
      : `${stage1Pct.toFixed(1)}% · ${scheduler.reachable === false ? "scheduler 상태 확인 필요" : `마지막 관측 active ${active} / cap ${cap}`}`,
  );
  readinessState(
    "stage1Readiness",
    result >= total
      ? "complete"
      : !schedulerFresh
        ? scheduler.reachable === false ? "failed" : "waiting"
        : active > 0 ? "running" : "waiting",
  );

  const quality = qualityGateState(data);
  const threshold = quality.threshold;
  const modelStageActive = state.currentId === "surrogate" && ["running", "ready"].includes(state.currentStatus);
  if (quality.available) {
    setText("r2ReadinessValue", `최소 R² ${decimal(quality.minR2, 4)}`);
    setText("r2ReadinessDetail", `${quality.passedCount} / ${quality.targetCount} 통과 · 목표 ≥ ${threshold.toFixed(2)}${quality.v4Active ? " · v4 authority" : ""}`);
  } else {
    setText("r2ReadinessValue", "공식 결과 대기");
    setText(
      "r2ReadinessDetail",
      quality.v4Active
        ? `v4 official Stage 1·R² authority 검증 대기 · 목표 ≥ ${threshold.toFixed(2)}`
        : `${quality.targetCount}개 지표 목표 ≥ ${threshold.toFixed(2)} · Stage 1 후 판정`,
    );
  }
  readinessState(
    "r2Readiness",
    quality.passed
      ? "complete"
      : quality.failed
        ? "failed"
        : modelStageActive
          ? "running"
          : "waiting",
  );

  const optimization = data.optimization || {};
  const approval = authorizationGateState(data);
  const specAudited = ["verified", "artifact_audited"].includes(optimization.spec_status);
  setText(
    "inputReadinessValue",
    approval.approved
      ? "Authorization 완료"
      : approval.rejected
        ? "승인 무효"
        : approval.confirmed
          ? "입력 확인 완료 · authorization 대기"
          : "사용자 승인 필요",
  );
  setText(
    "inputReadinessDetail",
    approval.approved
      ? approval.v4Active ? "v4 authorization verified · Production 실행 허용" : "운전점·duty·권선 가정 확정"
      : approval.confirmed
        ? approval.receiptPresent
          ? "authorization receipt 확인됨 · 최종 authorized 상태 대기"
          : "입력 confirmation verified · authorization receipt 대기"
        : approval.v4Active
          ? `${approval.declarationStatus ? `declaration ${approval.declarationStatus}` : "declaration 대기"} · 입력 confirmation 필요`
          : `${specAudited ? "spec 감사 완료" : "spec 확인 필요"} · 운전점·duty·권선 가정`,
  );
  readinessState("inputReadiness", approval.approved ? "complete" : approval.rejected ? "failed" : "waiting");

  const optimizationStage = state.stages.find((stage) => stage.id === "optimization") || {};
  const optimizationStatus = optimizationStage.status || "waiting";
  const optimizationNeeds = [];
  if (result < total) optimizationNeeds.push(`Stage 1 ${total - result}건`);
  if (!quality.passed) optimizationNeeds.push(quality.v4Active ? "공식 R² authority" : "R² gate");
  if (!approval.approved) optimizationNeeds.push(approval.confirmed ? "authorization" : "입력 승인");
  const optimizationBlocked = optimizationNeeds.length > 0;
  const executionContradiction = optimizationBlocked && ["running", "complete"].includes(optimizationStatus);
  const optimizationFailed = quality.failed
    || approval.rejected
    || executionContradiction
    || ["failed", "unavailable"].includes(optimizationStatus);
  const optimizationLabel = optimizationBlocked
    ? executionContradiction
      ? "관문 상태 불일치"
      : optimizationFailed
        ? "관문 확인 필요"
        : "상위 gate 대기"
    : {
        complete: "최적화 완료",
        running: "NSGA-II 실행 중",
        ready: "실행 준비 완료",
      }[optimizationStatus] || "실행 준비 확인 중";
  setText("optimizationReadinessValue", optimizationLabel);
  setText(
    "optimizationReadinessDetail",
    optimizationNeeds.length ? `${optimizationNeeds.join(" + ")} 필요` : "Pareto FEA와 target-load 검증으로 연결",
  );
  readinessState(
    "optimizationReadiness",
    optimizationBlocked
      ? optimizationFailed ? "failed" : "waiting"
      : optimizationStatus === "complete"
      ? "complete"
      : ["failed", "unavailable"].includes(optimizationStatus)
        ? "failed"
        : ["running", "ready"].includes(optimizationStatus)
          ? "running"
          : "waiting",
  );

  const blocker = blockerState(data, state);
  if (!schedulerFresh && result < total) {
    setText(
      "heroWaitTitle",
      !snapshotFresh
        ? "대시보드 snapshot 갱신 지연"
        : !topLevelFresh
          ? "대시보드 상태 확인 필요"
          : scheduler.reachable === false ? "Scheduler 연결 확인 필요" : "Scheduler 상태 갱신 지연",
    );
    setText(
      "heroWaitDetail",
      !snapshotFresh
        ? `Stage 1 잔여 ${total - result}건 · 오래된 snapshot의 active 수를 실행 상태로 해석하지 않습니다.`
        : !topLevelFresh
          ? `Stage 1 잔여 ${total - result}건 · health=${data.health || "unknown"} 상태가 회복될 때까지 기다립니다.`
          : scheduler.reachable === false
        ? `Stage 1 잔여 ${total - result}건 · 연결 복구 전 active 수를 실행 상태로 해석하지 않습니다.`
        : `Stage 1 잔여 ${total - result}건 · 마지막 관측 active ${active} / cap ${cap} · fresh 상태를 기다립니다.`,
    );
  } else if (result < total) {
    setText("heroWaitTitle", `Stage 1 검증 결과 ${total - result}건 수집 중`);
    setText("heroWaitDetail", `현재 active ${active} / cap ${cap} · 완료 후 공식 9-target R² gate를 실행합니다.`);
  } else if (quality.failed) {
    setText(
      "heroWaitTitle",
      quality.authorityVerified ? `R² ${threshold.toFixed(2)} 품질 기준 미충족` : "v4 공식 R² authority 검증 실패",
    );
    setText(
      "heroWaitDetail",
      quality.authorityVerified
        ? `현재 최소 R² ${decimal(quality.minR2, 4)} · 보강 단계 판정 후 다시 평가합니다.`
        : "contract 또는 official Stage 1 authority가 유효하지 않아 R² gate를 완료로 간주하지 않습니다.",
    );
  } else if (!quality.available) {
    setText("heroWaitTitle", quality.v4Active ? "v4 공식 Stage 1·R² authority" : "공식 9-target R² gate 산출물");
    setText("heroWaitDetail", quality.v4Active
      ? "official completion과 R² authority가 모두 verified된 gate 결과를 기다립니다."
      : `Stage 1 감사·학습·독립 test 평가 후 최소 R² ${threshold.toFixed(2)}를 확인합니다.`);
  } else if (!approval.approved) {
    setText("heroWaitTitle", approval.confirmed ? "Production authorization receipt" : "Production 최적화 입력 승인");
    setText("heroWaitDetail", approval.confirmed
      ? "입력 confirmation은 verified됐지만 authorization.status=authorized 증거 전에는 NSGA-II를 시작하지 않습니다."
      : "정격 운전점·DC 전압·전류·권선·duty 가정을 확정해야 NSGA-II를 시작합니다.");
  } else {
    setText("heroWaitTitle", blocker.summary);
    setText("heroWaitDetail", blocker.detail);
  }
}

function renderOverview(data) {
  const state = overallState(data);
  const progressRuntime = state.progressRuntime || state.runtime;
  const counter = runtimeCounter(progressRuntime);
  const composition = schedulerComposition(state.runtime.schedulerCounts);
  const currentParts = [counter, state.currentDetail].filter(Boolean);
  if (composition.active || composition.completed || composition.failed) {
    currentParts.push(`scheduler 활성 ${composition.active} · 완료 ${composition.completed} · 실패 ${composition.failed}`);
  }

  setText("resolvedStages", `해결된 단계 ${state.resolved} / ${state.totalStages}`);
  setText("heroTitle", `${state.currentLabel} · ${statusKorean[state.currentStatus] || state.currentStatus}`);
  setText("heroDescription", `${state.currentDetail} 해결된 단계는 ${state.resolved}/${state.totalStages}이며, 아래 진행률은 현재 단계만 표시합니다.`);
  setText("heroPercent", progressRuntime.progressPct === null ? "—" : `${progressRuntime.progressPct.toFixed(1)}%`);
  setText("heroPercentLabel", progressRuntime.progressSource === "live_runner"
    ? `${state.currentLabel} · live runner 검증`
    : state.currentLabel);
  const progress = byId("heroProgress");
  progress.setAttribute("aria-label", `${state.currentLabel}${progressRuntime.progressSource === "live_runner" ? " live runner 검증" : ""} 진행률`);
  if (progressRuntime.completed !== null && progressRuntime.total !== null && progressRuntime.total > 0) {
    progress.max = progressRuntime.total;
    progress.value = Math.min(progressRuntime.completed, progressRuntime.total);
    setText("heroProgressLabel", counter);
  } else if (progressRuntime.progressPct !== null) {
    progress.max = 100;
    progress.value = progressRuntime.progressPct;
    setText("heroProgressLabel", `${progressRuntime.progressPct.toFixed(1)}%`);
  } else {
    progress.max = 1;
    progress.value = 0;
    setText("heroProgressLabel", "단계 수치 대기");
  }

  setText("currentSummary", `${state.currentLabel} · ${statusKorean[state.currentStatus] || state.currentStatus}`);
  setText("currentDetail", currentParts.join(" · "));
  setText("nextSummary", state.nextLabel);
  setText("nextDetail", state.nextDetail);
  const blocker = blockerState(data, state);
  setText("blockerSummary", blocker.summary);
  setText("blockerDetail", blocker.detail);
  renderHeroOperations(data, state);
}

function renderCampaign(data) {
  const campaign = data.campaign || {};
  const scheduler = data.scheduler || {};
  const current = overallState(data);
  const total = integer(campaign.total, 700);
  const result = integer(campaign.result_ok);
  const active = scheduler.reachable ? integer(scheduler.active_count) : integer(campaign.active);
  const cap = integer(scheduler.cap, integer(campaign.cap, 100));
  const collectionVerified = campaign.completion_source === "atomic_collection"
    && campaign.collection_integrity_status === "verified";
  const schedulerCounts = scheduler.status_counts || {};
  const running = integer(schedulerCounts.running);
  const assigning = integer(schedulerCounts.queued) + integer(schedulerCounts.attaching);
  const snapshotFresh = timestampAgeMs(data.generated_at) <= SNAPSHOT_STALE_MS;
  const topLevelFresh = data.stale !== true
    && ["running", "healthy", "ok", "idle"].includes(String(data.health || "").trim().toLowerCase());
  const collectionFresh = collectionVerified && snapshotFresh && topLevelFresh;
  const schedulerFresh = scheduler.reachable === true
    && scheduler.stale !== true
    && snapshotFresh
    && topLevelFresh;
  setText("resultOk", result.toLocaleString("ko-KR"));
  setText("resultTotal", `/ ${total.toLocaleString("ko-KR")}`);
  setText(
    "resultSub",
    collectionVerified
      ? collectionFresh
        ? `authoritative atomic collection ${result} / ${total} 검증 완료 · runner 로그는 과거 기록(비권위)`
        : `마지막 검증값 ${result} / ${total} · snapshot 갱신 필요`
      : `scheduler 완료 ${integer(campaign.scheduler_ok)}건 · 안정화 ${integer(campaign.settling_results)}건`,
  );
  setText("activeSlots", schedulerFresh ? active.toLocaleString("ko-KR") : "—");
  setText("activeCap", `/ ${cap}`);
  setText(
    "slotSub",
    schedulerFresh
      ? `실행 ${running} · 배정/대기 ${assigning}`
      : `${scheduler.reachable === false ? "Scheduler 연결 확인 필요" : "상태 갱신 지연"} · 마지막 관측 ${active} / ${cap}`,
  );
  if (["stage2", "stage3"].includes(current.currentId)) {
    const counts = current.runtime.schedulerCounts || {};
    const completedTasks = integer(counts.completed);
    const runningTasks = integer(counts.running);
    const queuedTasks = integer(counts.queued) + integer(counts.attaching);
    const failedTasks = integer(counts.failed);
    const runner = current.runtime.runnerProgress || {};
    const runnerCount = (value) => value === null || value === undefined
      ? "—"
      : value.toLocaleString("ko-KR");
    // Remediation proof is separate from both Slurm completion and collected rows.
    const torqueReplay = data.torque_unit_replay || {};
    const torqueReplayPlanned = integer(torqueReplay.planned);
    const torqueReplayFailed = integer(torqueReplay.failed);
    const torqueReplayFailedAttempts = integer(torqueReplay.failed_attempts);
    const torqueReplayRetryDetail = torqueReplayFailedAttempts > torqueReplayFailed
      ? ` · 재시도 이력 ${torqueReplayFailedAttempts}`
      : "";
    const torqueReplayDetail = torqueReplayPlanned > 0
      ? ` · 단위 재검증 ${integer(torqueReplay.completed)}/${torqueReplayPlanned}`
        + ` (실행 ${integer(torqueReplay.active)}, 실패 ${torqueReplayFailed}${torqueReplayRetryDetail})`
      : "";
    setText("rateLabel", `${current.currentLabel} 로컬 최종 수집`);
    setText(
      "completionRate",
      current.runtime.completed === null
        ? "—"
        : current.runtime.completed.toLocaleString("ko-KR"),
    );
    setText(
      "completionRateUnit",
      current.runtime.total === null
        ? current.runtime.unit || "건"
        : `/ ${current.runtime.total.toLocaleString("ko-KR")}`,
    );
    setText(
      "rateSub",
      runner.available
        ? `runner 검증 ${runnerCount(runner.resultOk)}/${runnerCount(runner.total)} · `
          + `결과 감사 대기 ${runnerCount(runner.auditPending)} · 제출 ${runnerCount(runner.submitted)} · `
          + `실행 ${runnerCount(runner.active)} · 잔여 ${runnerCount(runner.missing)} · `
          + `Slurm 원시 완료 ${completedTasks} · 실패 이력 ${failedTasks}${torqueReplayDetail}`
        : `runner 진행 미확인 · Slurm 원시 완료 ${completedTasks} · `
          + `실패 이력 ${failedTasks}${torqueReplayDetail}`,
    );
    setText("etaLabel", `${current.currentLabel} 실행 중`);
    setText("etaValue", schedulerFresh ? runningTasks.toLocaleString("ko-KR") : "—");
    setText("etaUnit", "건");
    setText(
      "etaSub",
      schedulerFresh
        ? `Slurm 원시 배정/대기 ${queuedTasks} · 실패 이력 ${failedTasks}`
        : `마지막 관측 실행 ${runningTasks} · 상태 갱신 필요`,
    );
  } else {
    setText("rateLabel", "Stage 1 최근 처리 속도");
    setText("completionRate", decimal(campaign.completion_rate_per_hour, 1));
    setText("completionRateUnit", "건/시간");
    setText("rateSub", "최근 최대 6시간 · 검증 완료 증가량 기준");
    const eta = durationHours(campaign.eta_hours);
    setText("etaLabel", "Stage 1 예상 잔여");
    setText("etaValue", eta.value);
    setText("etaUnit", eta.unit);
    setText("etaSub", finite(campaign.eta_hours)
      ? `${estimatedFinish(campaign.eta_hours)} 예상 · 후속 단계 제외`
      : "완료 표본이 더 필요합니다 · 후속 단계 제외");
  }
}

function renderPipeline(data) {
  const state = overallState(data);
  const stages = state.stages;
  const governanceActive = governanceContext(data).active;
  const quality = qualityGateState(data);
  const authorization = authorizationGateState(data);
  const campaign = data.campaign || {};
  const stage1Incomplete = integer(campaign.result_ok) < Math.max(1, integer(campaign.total, 700));
  const optimizationBlocked = governanceActive && (stage1Incomplete || !quality.passed || !authorization.approved);
  const optimizationEvidenceFailed = quality.failed || authorization.rejected;
  setText(
    "currentStageChip",
    state.currentId === "optimization" && optimizationBlocked
      ? `최적화 governance gate ${optimizationEvidenceFailed ? "확인 필요" : "대기"} · ${state.resolved}/${state.totalStages}`
      : `${state.currentLabel} · ${state.resolved}/${state.totalStages}`,
  );
  const list = byId("pipelineList");
  empty(list);
  stages.forEach((stage, index) => {
    const effectiveStatus = stage.status || "waiting";
    const item = element("li", `pipeline-item ${effectiveStatus}`);
    item.appendChild(element("span", "stage-node", effectiveStatus === "complete" ? "✓" : String(index + 1).padStart(2, "0")));
    const copy = element("div", "stage-copy");
    copy.appendChild(element("strong", "", stage.label || "—"));
    copy.appendChild(element("small", "", stage.detail || ""));
    if (stage.id === "optimization" && optimizationBlocked) {
      copy.appendChild(element("small", "stage-runtime-tasks", "v4 공식 R² authority와 authorization gate 충족 전 실행 차단"));
    }
    const runtime = stage.id === state.currentId ? state.runtime : stageRuntime(stage);
    const taskComposition = schedulerComposition(runtime.schedulerCounts);
    if (taskComposition.active || taskComposition.completed || taskComposition.failed) {
      copy.appendChild(element("small", "stage-runtime-tasks", `FEA 활성 ${taskComposition.active} · 완료 ${taskComposition.completed} · 실패 ${taskComposition.failed}`));
    }
    item.appendChild(copy);
    const meta = element("div", "stage-meta");
    meta.appendChild(element("span", "stage-status", statusKorean[effectiveStatus] || effectiveStatus));
    const counterText = runtimeCounter(runtime);
    if (counterText) meta.appendChild(element("small", "stage-counter", counterText));
    item.appendChild(meta);
    list.appendChild(item);
  });
}

function renderScheduler(data) {
  const scheduler = data.scheduler || {};
  const counts = scheduler.status_counts || {};
  const health = byId("schedulerHealth");
  const historyReturned = count(scheduler.history_returned_count);
  const historyComplete = scheduler.history_complete;
  const identityOk = scheduler.project_exists === true
    && scheduler.project_matches !== false
    && scheduler.cap_matches !== false;
  if (scheduler.reachable && !scheduler.stale && identityOk && historyComplete !== false) {
    health.textContent = "정상 연결";
    health.className = "health-pill complete";
  } else if (scheduler.reachable && identityOk && historyComplete === false) {
    health.textContent = "이력 부분 조회";
    health.className = "health-pill warning";
  } else if (scheduler.reachable && !identityOk) {
    health.textContent = "프로젝트 확인 필요";
    health.className = "health-pill failed";
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
  const projectTotal = count(scheduler.project_total_count);
  const history = byId("schedulerHistory");
  if (historyComplete === true) {
    history.textContent = `Task 이력 전체 조회 · ${(historyReturned ?? projectTotal ?? 0).toLocaleString("ko-KR")} / ${(projectTotal ?? historyReturned ?? 0).toLocaleString("ko-KR")}건`;
    history.className = "history-coverage complete";
  } else if (historyComplete === false) {
    history.textContent = `Task 이력 부분 조회 · ${historyReturned === null ? "—" : historyReturned.toLocaleString("ko-KR")} / ${projectTotal === null ? "—" : projectTotal.toLocaleString("ko-KR")}건 · 합계를 완전한 이력으로 해석하지 않음`;
    history.className = "history-coverage partial";
  } else {
    history.textContent = "Task 이력 범위 미보고 · 기존 v1 snapshot";
    history.className = "history-coverage warning";
  }
  setText("taskTotal", historyReturned !== null
    ? `표시 이력 ${historyReturned.toLocaleString("ko-KR")}건${projectTotal !== null ? ` / project ${projectTotal.toLocaleString("ko-KR")}건` : ""} · 재시도 포함`
    : `project task ${integer(scheduler.project_total_count).toLocaleString("ko-KR")}건 · 이력 범위 확인 대기`);
  setText("schedulerProject", scheduler.project || data.project || "—");
  const projectIdentity = byId("projectIdentity");
  const deploymentText = integer(scheduler.deployment_count) > 0
    ? ` · 배포 ${integer(scheduler.deployed_count)}/${integer(scheduler.deployment_count)}`
    : "";
  if (identityOk) {
    projectIdentity.textContent = `#${integer(scheduler.project_id)} 등록·일치 · cap ${integer(scheduler.server_cap, integer(scheduler.cap))}${deploymentText}`;
    projectIdentity.className = "";
  } else {
    projectIdentity.textContent = scheduler.project_exists ? "project명 또는 cap 불일치" : "scheduler project 없음";
    projectIdentity.className = "failed";
  }
  const nodes = Array.isArray(scheduler.nodes) ? scheduler.nodes : [];
  const computeNodes = nodes.filter((node) => node.node !== "배정 대기");
  const queuedTasks = nodes
    .filter((node) => node.node === "배정 대기")
    .reduce((sum, node) => sum + integer(node.active_tasks), 0);
  setText("nodeCount", `${computeNodes.length} compute nodes${queuedTasks ? ` · 대기 ${queuedTasks}` : ""}`);
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

function renderQualityProfileExperiment(data) {
  const experiment = record(data.quality_profile_experiment);
  const expected = Math.max(1, integer(experiment.expected_cases, 24));
  const planned = count(experiment.planned);
  const active = count(experiment.active);
  const completed = count(experiment.completed);
  const failed = count(experiment.failed);
  const missing = count(experiment.missing);
  const relationVerified = experiment.official_pipeline_stage === false
    && experiment.official_speed_stage === false
    && experiment.relation_to_official_speed === "separate_from_post_pareto_speed_validation";
  const trusted = experiment.integrity_status === "verified"
    && experiment.scheduler_integrity_status === "verified"
    && experiment.scheduler_trusted === true
    && experiment.history_complete === true
    && data.stale !== true
    && relationVerified;
  const invalid = experiment.integrity_status === "invalid"
    || experiment.plan_integrity_status === "invalid"
    || experiment.scheduler_integrity_status === "invalid"
    || !relationVerified;
  const partial = experiment.scheduler_integrity_status === "partial_history";
  const rawStatus = trusted ? String(experiment.status || "ready") : invalid ? "invalid" : "unavailable";
  const card = byId("qualityExperimentCard");
  card.dataset.state = rawStatus;

  const status = byId("qualityExperimentStatus");
  const statusLabel = {
    complete: "분석 완료",
    collecting: "결과 수집 중",
    analyzing: "A/B 분석 중",
    running: "실행 중",
    failed: "실패 case 있음",
    ready: "제출 대기",
    invalid: "identity 검증 실패",
    unavailable: partial ? "부분 이력 · 판정 보류" : "상태 확인 필요",
  }[rawStatus] || "상태 확인 필요";
  status.textContent = statusLabel;
  status.className = `health-pill ${rawStatus === "complete" ? "complete" : ["running", "collecting", "analyzing"].includes(rawStatus) ? "running" : rawStatus === "failed" || rawStatus === "invalid" ? "failed" : "warning"}`;

  const progress = byId("qualityExperimentProgress");
  progress.max = expected;
  progress.value = trusted ? Math.min(expected, completed ?? 0) : 0;
  setText(
    "qualityExperimentProgressLabel",
    trusted
      ? `${(completed ?? 0).toLocaleString("ko-KR")} / ${expected.toLocaleString("ko-KR")} complete · ${decimal(experiment.progress_pct, 1)}%`
      : `${planned === null ? "—" : planned.toLocaleString("ko-KR")} planned · 진행률 판정 보류`,
  );
  setText("qualityExperimentPlanned", planned === null ? "—" : planned.toLocaleString("ko-KR"));

  const observedCount = (value) => value === null
    ? "—"
    : trusted
      ? value.toLocaleString("ko-KR")
      : partial
        ? `${value.toLocaleString("ko-KR")} 관측`
        : "—";
  setText("qualityExperimentActive", observedCount(active));
  setText("qualityExperimentCompleted", observedCount(completed));
  setText("qualityExperimentFailed", observedCount(failed));
  setText("qualityExperimentMissing", trusted && missing !== null ? missing.toLocaleString("ko-KR") : "—");

  const schedulerCounts = record(experiment.scheduler_status_counts);
  setText(
    "qualityExperimentSchedulerCounts",
    `${trusted ? "정확한 task prefix 집계" : partial ? "부분 이력 관측값" : "집계 검증 보류"} · queued ${integer(schedulerCounts.queued)} · attaching ${integer(schedulerCounts.attaching)} · running ${integer(schedulerCounts.running)} · completed ${integer(schedulerCounts.completed)} · failed ${integer(schedulerCounts.failed)} · cancelled ${integer(schedulerCounts.cancelled)}`,
  );
  const profiles = Array.isArray(experiment.profiles) ? experiment.profiles : [];
  setText(
    "qualityExperimentProfiles",
    experiment.plan_integrity_status === "verified" && profiles.length === 2
      ? `${profiles[0]} ↔ ${profiles[1]} · ${integer(experiment.source_count, integer(experiment.expected_sources, 12))} paired sources`
      : "Profile pair identity 검증 필요",
  );
  const chosenCandidate = typeof experiment.chosen_candidate === "string"
    && profiles.includes(experiment.chosen_candidate)
    ? experiment.chosen_candidate
    : "";
  const analysisVerified = experiment.analysis_integrity_status === "verified"
    && integer(experiment.analysis_outputs_verified) === 4;
  const conclusion = {
    complete: chosenCandidate && analysisVerified
      ? `Conclusion: ${chosenCandidate}가 strict 12/12 A/B gate에서 선택됨`
      : "Conclusion: 분석 무결성 재검증 필요",
    analyzing: "Conclusion: 24/24 FEA 수집 완료 · strict A/B ranking 진행 중",
    collecting: "Conclusion: scheduler 24/24 완료 · atomic collection 게시 대기",
    running: "Conclusion: paired FEA 실행 중 · 아직 profile을 선택하지 않음",
    ready: "Conclusion: scheduler 제출 대기 · 아직 profile을 선택하지 않음",
    failed: "Conclusion: 실험 또는 산출물 무결성 실패 · 선택 무효",
    invalid: "Conclusion: identity/manifest/hash 검증 실패 · 선택 무효",
    unavailable: "Conclusion: 신뢰 가능한 전체 이력 확인 전 판정 보류",
  }[rawStatus] || "Conclusion: 판정 보류";
  setText("qualityExperimentConclusion", conclusion);
  setText(
    "qualityExperimentSelectedProfile",
    rawStatus === "complete" && chosenCandidate && analysisVerified
      ? `Selected profile: ${chosenCandidate} · canonical manifest + 4 artifacts verified`
      : "Selected profile: — · verified analysis 완료 후에만 표시",
  );
  const collectionIntegrity = String(experiment.collection_integrity_status || "not_checked");
  const analysisIntegrity = String(experiment.analysis_integrity_status || "not_checked");
  setText(
    "qualityExperimentAnalysis",
    `Collection ${collectionIntegrity} · analysis ${analysisIntegrity} · verified artifacts ${integer(experiment.analysis_outputs_verified)} / 4 · official post-Pareto speed와 별도`,
  );

  const projectActive = count(experiment.project_active);
  const projectCap = count(experiment.project_cap);
  const openSlots = count(experiment.project_open_slots);
  const capRelation = projectActive !== null && projectCap !== null
    ? `${trusted ? "" : "마지막 관측 · "}Project active ${projectActive.toLocaleString("ko-KR")} / cap ${projectCap.toLocaleString("ko-KR")} · 이 실험 active ${active === null ? "—" : active.toLocaleString("ko-KR")} (${decimal(experiment.experiment_active_share_pct, 1)}%) · open ${openSlots === null ? "—" : openSlots.toLocaleString("ko-KR")}`
    : "Project cap 관계 확인 필요";
  setText("qualityExperimentCap", capRelation);
}

function renderCheckpoint(data) {
  const checkpoint = data.checkpoint || data.scheduler?.checkpoint || {};
  const nestedPreview = record(checkpoint.diagnostic_preview);
  const preview = isDiagnosticPreview(checkpoint) ? checkpoint : nestedPreview;
  const previewPresent = isDiagnosticPreview(preview);
  const splitSummary = byId("checkpointSplits");
  setText(
    "checkpointTitle",
    previewPresent ? preview.stage || "Stage1 preview (비공식)" : "60-design 조기 Surrogate 체크포인트",
  );
  splitSummary.hidden = previewPresent;
  if (previewPresent) {
    const expectedRows = Math.max(1, integer(preview.expected_rows, preview.validation_rows));
    const validationRows = Math.max(0, integer(preview.validation_rows));
    const progress = byId("checkpointProgress");
    progress.max = expectedRows;
    progress.value = preview.available === true ? Math.min(expectedRows, validationRows) : 0;
    setText(
      "checkpointProgressLabel",
      preview.available === true
        ? `${validationRows} / ${expectedRows} validated rows`
        : `0 / ${expectedRows} verified rows`,
    );
    const status = byId("checkpointStatus");
    status.textContent = preview.available === true ? "진단 완료 (비공식)" : "preview 감사 실패";
    status.className = `health-pill ${preview.available === true ? "warning" : "failed"}`;
    setText(
      "checkpointNote",
      preview.available === true
        ? `DIAGNOSTIC ONLY · ${validationRows}/${expectedRows} validation ${preview.validation_status || "verified"} · R² 최소 ${decimal(preview.min_r2, 4)} · 평균 ${decimal(preview.avg_r2, 4)} · 통과 ${integer(preview.passed_count)}/${integer(preview.target_count)} · 공식 gate 아님 · optimization gate 영향 없음`
        : "DIAGNOSTIC ONLY · preview schema/path/hash 감사를 통과하지 못했습니다. 공식 gate와 optimization gate는 닫혀 있습니다.",
    );
    const worst = byId("checkpointWorst");
    empty(worst);
    const metrics = Array.isArray(preview.metrics) ? [...preview.metrics] : [];
    metrics
      .filter((metric) => finite(metric.r2))
      .sort((left, right) => left.r2 - right.r2)
      .slice(0, 3)
      .forEach((metric) => {
        worst.appendChild(element("b", "", `${metric.label || metric.target}: ${decimal(metric.r2, 4)}`));
      });
    if (!metrics.length) worst.appendChild(element("small", "", "검증된 preview 지표 없음"));
    setText(
      "checkpointAction",
      "다음: 공식 Stage 1 R² authority 복구/검증 · preview로 최적화 금지",
    );
    return;
  }

  const execution = checkpoint.execution || {};
  const target = Math.max(1, integer(checkpoint.target_designs, 60));
  const liveComplete = Math.max(0, integer(checkpoint.complete_designs));
  const settling = Math.max(0, integer(checkpoint.settling_designs));
  const remaining = Math.max(0, integer(checkpoint.remaining_designs, target - liveComplete));
  const published = execution.status === "complete" && integer(execution.snapshot_designs) > 0;
  const complete = published ? integer(execution.snapshot_designs) : liveComplete;
  const splits = published ? execution.split_design_counts || {} : checkpoint.split_design_counts || {};
  const requirements = checkpoint.split_requirements || { train: 30, calibration: 10, test: 10 };
  const progress = byId("checkpointProgress");
  progress.max = target;
  progress.value = Math.min(target, complete);
  setText("checkpointProgressLabel", `${complete} / ${target} designs`);
  setText("checkpointTrain", `${integer(splits.train)} / ${integer(requirements.train, 30)}`);
  setText("checkpointCalibration", `${integer(splits.calibration)} / ${integer(requirements.calibration, 10)}`);
  setText("checkpointTest", `${integer(splits.test)} / ${integer(requirements.test, 10)}`);

  const status = byId("checkpointStatus");
  const statusKey = execution.status === "running" || execution.status === "complete" || execution.status === "resume_required"
    ? execution.status
    : checkpoint.status || "unavailable";
  const phaseLabel = {
    snapshot_fetch: "360-row snapshot 수집",
    validation: "데이터 검증",
    training: "Surrogate 학습",
    model_audit: "모델 감사",
    finalizing: "결과 게시",
  }[execution.phase] || "조기 진단 실행";
  const statusLabel = {
    ready: "최소 조건 충족",
    running: phaseLabel,
    complete: "조기 진단 완료",
    resume_required: "재개 확인 필요",
    settling: "결과 안정화",
    waiting: "FEA 수집 중",
    unavailable: "확인 필요",
  }[statusKey] || "확인 필요";
  status.textContent = statusLabel;
  status.className = `health-pill ${["ready", "complete"].includes(statusKey) ? "complete" : ["unavailable", "resume_required"].includes(statusKey) ? "failed" : "warning"}`;

  if (statusKey === "complete") {
    setText("checkpointNote", `고정 snapshot ${complete} designs / ${integer(execution.snapshot_rows)} rows · primary R² 최소 ${decimal(execution.primary_min_r2, 4)} · 평균 ${decimal(execution.primary_avg_r2, 4)} · 통과 ${integer(execution.primary_passed_count)}/8 · 전압 ${decimal(execution.voltage_r2, 4)} · 공식 gate 아님`);
  } else if (statusKey === "running") {
    setText("checkpointNote", `${phaseLabel} 진행 중 · exact ${complete} designs / ${integer(checkpoint.complete_base_rows)} base rows · 공식 gate와 격리됨`);
  } else if (statusKey === "resume_required") {
    setText("checkpointNote", "부분 산출물 또는 PID marker를 감사한 뒤 안전하게 재개해야 합니다. 공식 gate는 영향받지 않습니다.");
  } else if (statusKey === "ready") {
    setText("checkpointNote", `provisional 진단 최소 조건 충족 · ${integer(checkpoint.complete_base_rows)}개 base row · 공식 R² gate와 분리됨`);
  } else if (statusKey === "settling") {
    setText("checkpointNote", `${settling}개 완전 설계의 결과 안정화 확인 중 · ${remaining} designs remaining · 공식 gate 아님`);
  } else if (statusKey === "waiting") {
    setText("checkpointNote", `${remaining} designs remaining · ${integer(checkpoint.complete_base_rows)}개 base row 안정화 · scope ${checkpoint.diagnostic_scope || "physics_only"}`);
  } else {
    setText("checkpointNote", "체크포인트 상태를 확인할 수 없습니다. 다음 scheduler 갱신에서 다시 시도합니다.");
  }
  const worst = byId("checkpointWorst");
  empty(worst);
  const metrics = Array.isArray(execution.primary_metrics) ? execution.primary_metrics : [];
  metrics.slice(0, 3).forEach((metric) => {
    worst.appendChild(element("b", "", `${provisionalLabels[metric.target] || metric.target}: ${decimal(metric.r2, 4)}`));
  });
  if (!metrics.length) worst.appendChild(element("small", "", statusKey === "complete" ? "상세 지표 확인 필요" : "체크포인트 완료 후 표시"));
  const action = {
    continue_stage1: "다음: Stage 1 700-row 수집 계속 · 공식 gate 판정 대기",
    run_stage2: "다음: 공식 Stage 1 gate까지 계속 수집",
  }[execution.recommended_action] || "다음: 공식 Stage 1 gate까지 계속 수집";
  setText("checkpointAction", action);
}

function familyRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function familyToken(value) {
  if (typeof value === "string") return value.trim().toLowerCase();
  const record = familyRecord(value);
  return familyToken(record.status || record.state || record.decision || "");
}

function firstFinite(...values) {
  return values.find((value) => finite(value));
}

function familyMetricValue(family, cohort, metricName) {
  const metrics = family.metrics;
  const metricsRecord = familyRecord(metrics);
  const summary = familyRecord(family.summary || metricsRecord.summary);
  const cohortAliases = cohort === "baseline"
    ? ["baseline", "baseline_control"]
    : ["selected", "selected_families", "selected_family"];
  const metricAliases = {
    primary_min_r2: ["primary_min_r2", "primary_min", "min_r2"],
    primary_avg_r2: ["primary_avg_r2", "primary_avg", "avg_r2"],
    voltage_r2: ["voltage_r2", "voltage"],
  }[metricName];

  for (const cohortAlias of cohortAliases) {
    for (const source of [
      familyRecord(metricsRecord[cohortAlias]),
      familyRecord(summary[cohortAlias]),
      familyRecord(family[cohortAlias]),
    ]) {
      const value = firstFinite(...metricAliases.map((alias) => source[alias]));
      if (value !== undefined) return value;
    }
  }
  for (const source of [metricsRecord, summary, family]) {
    const flatKeys = cohortAliases.flatMap((cohortAlias) => metricAliases.map((alias) => `${cohortAlias}_${alias}`));
    const value = firstFinite(...flatKeys.map((key) => source[key]));
    if (value !== undefined) return value;
  }
  if (Array.isArray(metrics)) {
    for (const row of metrics) {
      const item = familyRecord(row);
      const key = familyToken(item.key || item.name || item.metric || item.target);
      if (!metricAliases.includes(key) && !metricAliases.some((alias) => key.endsWith(alias))) continue;
      const value = firstFinite(
        item[cohort],
        item[`${cohort}_r2`],
        familyRecord(item.values)[cohort],
      );
      if (value !== undefined) return value;
    }
  }
  return undefined;
}

function familyConfirmationState(family) {
  const status = familyToken(family.status);
  const phase = familyToken(family.phase);
  const decision = familyToken(family.decision || family.confirmation_status || family.result_status);
  const integrity = familyToken(family.integrity || family.integrity_status);
  const terminal = ["positive_confirmation", "negative_confirmation", "invalid"];
  if ((family.diagnostic_only !== undefined && family.diagnostic_only !== true)
    || (family.official_gate_eligible !== undefined && family.official_gate_eligible !== false)) {
    return "artifact_invalid";
  }
  if (status === "artifact_invalid") return status;
  if (terminal.includes(status)) return status;
  if (["invalid", "corrupt", "tampered", "artifact_invalid"].includes(integrity)) return "artifact_invalid";
  if (status === "resume_required") return status;
  if (status === "finalizing" || phase === "finalizing") return "finalizing";
  if (status === "running") return status;
  if (["waiting", "waiting_stage1"].includes(status)) return "waiting";
  if (status === "completion_pending") return "finalizing";
  if (["complete", "already_complete"].includes(status)) {
    return terminal.includes(decision) ? decision : "finalizing";
  }
  if (terminal.includes(decision)) return decision;
  return "waiting";
}

function renderFamilyConfirmation(familyValue) {
  const family = familyRecord(familyValue);
  const state = familyConfirmationState(family);
  const card = byId("familyConfirmationCard");
  card.dataset.state = state;

  const phase = familyToken(family.phase);
  const phaseLabel = {
    stage1_results: "Stage 1 결과 대기",
    official_validation: "공식 validation 대기",
    official_training: "공식 학습 감사 대기",
    waiting_stage1: "Stage 1 결과 대기",
    ready: "독립 확인 준비",
    running: "모델 계열 비교 실행",
    training: "모델 계열 비교 학습",
    confirmation_starting: "모델 계열 비교 시작",
    confirmation_training: "모델 계열 비교 학습",
    completion_pending: "결과 무결성 최종 감사",
    finalizing: "결과 무결성 최종 감사",
  }[phase] || "Stage 1 감사 대기";
  const statusLabel = {
    waiting: phaseLabel,
    running: "독립 확인 실행 중",
    finalizing: "결과 최종 감사",
    resume_required: "안전 재개 필요",
    artifact_invalid: "진단 산출물 무효",
    positive_confirmation: "선택 계열 개선 확인",
    negative_confirmation: "개선 근거 미확인",
    invalid: "비교 유효성 불충족",
  }[state];
  const statusTone = state === "positive_confirmation"
    ? "positive"
    : ["running", "finalizing"].includes(state)
      ? "active"
      : ["resume_required", "artifact_invalid", "negative_confirmation", "invalid"].includes(state)
        ? "warning"
        : "pending";
  const status = byId("familyConfirmationStatus");
  if (status.textContent !== statusLabel) status.textContent = statusLabel;
  status.className = `confirmation-state-badge ${statusTone}`;

  const process = familyRecord(family.process);
  const processState = familyToken(
    process.state || process.status || process.mode || family.process_state || family.process,
  );
  let processLabel = {
    active: "프로세스 실행",
    alive: "프로세스 실행",
    running: "프로세스 실행",
    managed: "프로세스 관리됨",
    stale: "프로세스 재개 필요",
    resume_required: "프로세스 재개 필요",
    stopped: "프로세스 종료",
    complete: "프로세스 종료",
    exited: "프로세스 종료",
    absent: "프로세스 대기",
    inactive: "프로세스 대기",
    waiting: "프로세스 대기",
    unknown: "프로세스 확인 필요",
  }[processState] || (["running", "finalizing"].includes(state) ? "프로세스 확인 중" : "프로세스 대기");
  if (processState === "stopped" && state === "waiting") processLabel = "프로세스 대기";
  if (processState === "stopped" && state === "resume_required") processLabel = "프로세스 재개 필요";
  const integrity = familyRecord(family.integrity);
  const integrityState = familyToken(
    integrity.state || integrity.status || family.integrity_status || family.integrity,
  );
  const integrityLabel = {
    verified: "무결성 검증됨",
    valid: "산출물 구조 정상",
    audited: "무결성 검증됨",
    complete: "무결성 검증됨",
    checking: "무결성 감사 중",
    auditing: "무결성 감사 중",
    running: "무결성 감사 중",
    invalid: "무결성 오류",
    corrupt: "무결성 오류",
    tampered: "무결성 오류",
    artifact_invalid: "무결성 오류",
    pending: "무결성 대기",
    waiting: "무결성 대기",
  }[integrityState] || (state === "artifact_invalid" ? "무결성 오류" : "무결성 대기");
  const evidenceTone = ["invalid", "corrupt", "tampered", "artifact_invalid"].includes(integrityState)
    || ["stale", "resume_required"].includes(processState)
    ? "warning"
    : ["verified", "valid", "audited", "complete"].includes(integrityState)
      ? "verified"
      : ["active", "alive", "running", "managed"].includes(processState)
        ? "active"
        : "pending";
  const evidence = byId("familyConfirmationEvidence");
  evidence.textContent = `${processLabel} · ${integrityLabel}`;
  evidence.className = `confirmation-evidence-badge ${evidenceTone}`;

  const summaryText = {
    waiting: `${phaseLabel} · 공식 R² gate와 분리된 진단입니다.`,
    running: `${phaseLabel} 중입니다. 결과는 공식 모델 선택이나 전체 진행률을 바꾸지 않습니다.`,
    finalizing: "lock·report·completion hash를 재검증한 뒤 진단 결과를 게시합니다.",
    resume_required: "봉인된 부분 산출물을 감사한 뒤 같은 진단을 안전하게 이어야 합니다.",
    artifact_invalid: "진단 산출물의 경로 또는 hash 감사가 실패해 결과를 사용할 수 없습니다.",
    positive_confirmation: "동일 untouched cohort에서 선택 모델 계열의 동시 개선이 확인됐습니다.",
    negative_confirmation: "선택 모델 계열이 baseline 대비 모든 판정 조건을 동시에 개선하지 못했습니다.",
    invalid: "물리 또는 통계 유효성이 성립하지 않아 모델 계열 비교 결론을 사용할 수 없습니다.",
  }[state];
  setText("familyConfirmationSummary", summaryText);

  const resumed = state === "resume_required"
    || family.resumed === true
    || family.resume_required === true
    || process.resumed === true
    || process.resume === true
    || ["resume", "resumed", "stale", "resume_required"].includes(familyToken(process.mode || processState))
    || integer(family.resume_count) > 0;
  const resumeNote = byId("familyConfirmationResume");
  resumeNote.hidden = !resumed;
  resumeNote.textContent = state === "resume_required"
    ? "재개 필요: 기존 lock과 PID 상태를 검증한 뒤 --resume 경로로 이어갑니다."
    : "재개 실행: 검증된 기존 lock을 재사용해 중단 지점부터 이어서 처리했습니다.";

  setText("familyBaselineMin", decimal(familyMetricValue(family, "baseline", "primary_min_r2"), 4));
  setText("familyBaselineAvg", decimal(familyMetricValue(family, "baseline", "primary_avg_r2"), 4));
  setText("familyBaselineVoltage", decimal(familyMetricValue(family, "baseline", "voltage_r2"), 4));
  setText("familySelectedMin", decimal(familyMetricValue(family, "selected", "primary_min_r2"), 4));
  setText("familySelectedAvg", decimal(familyMetricValue(family, "selected", "primary_avg_r2"), 4));
  setText("familySelectedVoltage", decimal(familyMetricValue(family, "selected", "voltage_r2"), 4));

  const scienceNote = {
    positive_confirmation: "과학적 진단 결과입니다. 개선 확인은 공식 R² gate 통과를 의미하지 않습니다.",
    negative_confirmation: "과학적 경고입니다. 개선 미확인은 공식 R² 실패로 처리되지 않습니다.",
    invalid: "과학적 경고입니다. 유효성 불충족은 공식 R² 실패로 처리되지 않습니다.",
    artifact_invalid: "진단 무결성 경고입니다. 공식 R² gate와 전체 진행률에는 영향이 없습니다.",
    resume_required: "재개 상태는 진단 실행에만 적용되며 공식 Stage 상태를 변경하지 않습니다.",
  }[state] || "이 비교는 DIAGNOSTIC ONLY이며 공식 R² gate와 전체 진행률을 변경하지 않습니다.";
  setText("familyConfirmationNote", scienceNote);
  card.setAttribute("aria-label", `모델 계열 독립 확인: ${statusLabel}. 진단 전용이며 공식 gate가 아닙니다.`);
}

function renderModel(data) {
  const model = record(data.model);
  const quality = qualityGateState(data);
  const diagnosticAvailable = isDiagnosticPreview(model) && model.available === true;
  const displayThreshold = diagnosticAvailable && finite(model.threshold)
    ? model.threshold
    : quality.threshold;
  const displayPassedCount = diagnosticAvailable ? integer(model.passed_count) : quality.passedCount;
  const displayTargetCount = diagnosticAvailable
    ? Math.max(1, integer(model.target_count, Array.isArray(model.metrics) ? model.metrics.length : 0))
    : quality.targetCount;
  setText("r2Threshold", displayThreshold.toFixed(2));
  setText("r2Passed", diagnosticAvailable || quality.available ? displayPassedCount : "—");
  setText("r2Total", `/ ${displayTargetCount}`);
  const stateText = diagnosticAvailable
    ? `DIAGNOSTIC ONLY · ${model.stage || "Stage1 preview (비공식)"} · 공식 gate 아님`
    : quality.passed
      ? "모든 공식 지표가 품질 목표를 통과했습니다"
      : quality.failed
        ? quality.authorityVerified ? "일부 공식 지표가 R² 목표에 미달했습니다" : "공식 R² authority 무결성 확인이 필요합니다"
        : quality.v4Active ? "v4 공식 Stage 1 completion과 R² authority를 기다립니다" : "Stage 1 완료 후 학습됩니다";
  setText("modelState", stateText);
  setText("modelStats", diagnosticAvailable
    ? `${integer(model.validation_rows)} / ${integer(model.expected_rows)} validation ${model.validation_status || "verified"} · 모델 hash ${integer(model.artifact_hash_count)} / 7 감사 · 최소 R² ${decimal(model.min_r2, 4)} · 평균 R² ${decimal(model.avg_r2, 4)} · optimization gate 영향 없음`
    : quality.available
      ? `${quality.v4Active ? "v4 official Stage 1" : model.stage || "현재"} · 최소 R² ${decimal(quality.minR2, 4)} · 평균 R² ${decimal(quality.avgR2, 4)}`
      : quality.v4Active
        ? "legacy model 상태는 gate 판정에 사용하지 않습니다."
        : "독립 test split에서 8개 primary + 전압 1개를 평가합니다.");
  const list = byId("metricList");
  empty(list);
  const metrics = diagnosticAvailable
    ? Array.isArray(model.metrics) ? model.metrics : []
    : quality.v4Active ? [] : Array.isArray(model.metrics) ? model.metrics : [];
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
  if (quality.v4Active && !diagnosticAvailable) {
    const row = element("div", `metric-row ${quality.passed ? "pass" : quality.failed ? "fail" : "pending"}`);
    row.appendChild(element("span", "", "v4 공식 Stage 1"));
    const progress = document.createElement("progress");
    progress.max = 1;
    progress.value = finite(quality.minR2) ? Math.max(0, Math.min(1, quality.minR2)) : 0;
    progress.setAttribute("aria-label", "v4 공식 Stage 1 최소 R²");
    row.appendChild(progress);
    row.appendChild(element("strong", "", quality.available ? decimal(quality.minR2, 4) : "authority 대기"));
    list.appendChild(row);
  }
  renderFamilyConfirmation(data.family_confirmation);
}

function renderPhysics(data) {
  const beta = data.beta || {};
  const optimization = data.optimization || {};
  const quality = qualityGateState(data);
  const approval = authorizationGateState(data);
  const governanceBlocked = approval.diagnosticBlocked
    || (approval.v4Active && (!quality.passed || !approval.approved));
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
  const targetEvidence = `${decimal(optimization.target_torque_nm, 1)} N·m @ ${integer(optimization.target_torque_speed_rpm).toLocaleString("ko-KR")} rpm = ${decimal(optimization.torque_point_power_kw, 3)} kW · ${decimal(optimization.target_power_kw, 1)} kW @ ${integer(optimization.target_power_speed_rpm).toLocaleString("ko-KR")} rpm = ${decimal(optimization.power_point_torque_nm, 3)} N·m`;
  setText(
    "constraintNote",
    approval.diagnosticBlocked
      ? `DIAGNOSTIC ONLY preview · 공식 R² gate 아님 · Production NSGA 차단 · ${targetEvidence}`
      : approval.v4Active
      ? approval.approved && quality.passed
        ? `v4 공식 R² authority와 authorization 검증 완료 · ${targetEvidence}`
        : approval.rejected || quality.failed
          ? `v4 governance 검증 실패 · Production NSGA 차단 · ${targetEvidence}`
          : approval.confirmed
            ? `입력 확인 완료 · authorization 대기 · Production NSGA 차단 · ${targetEvidence}`
            : `v4 입력 confirmation 대기 · Production NSGA 차단 · ${targetEvidence}`
      : ["verified", "artifact_audited"].includes(optimization.spec_status)
        ? `산출물 감사 완료 · 사용자 확인 전 production NSGA 차단 · ${targetEvidence}`
        : "최적화 spec 확인 실패 · 화면에는 기본 목표값이 표시됩니다.",
  );
  const nsga = byId("nsgaProgress");
  empty(nsga);
  const seedProgress = governanceBlocked ? [] : Array.isArray(optimization.seeds) ? optimization.seeds : [];
  const configuredSeeds = Array.isArray(optimization.configured_seeds) ? optimization.configured_seeds : [42, 43, 44];
  const progressBySeed = new Map(seedProgress.map((item) => [integer(item.seed, -1), item]));
  if (governanceBlocked) {
    nsga.appendChild(element("p", "", "v4 공식 R² authority와 authorization gate 충족 전 NSGA-II 실행 차단"));
  } else if (seedProgress.length) {
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
  if (!governanceBlocked && (integer(optimization.pareto_candidates) || integer(optimization.fea_case_rows))) {
    nsga.appendChild(element("p", "", `Pareto ${integer(optimization.pareto_candidates)}개 · FEA 검증 행 ${integer(optimization.fea_case_rows)}개`));
  }
}

function renderTargetLoad(data) {
  const targetLoad = data.target_load || {};
  const quality = qualityGateState(data);
  const approval = authorizationGateState(data);
  const governanceBlocked = approval.v4Active && (!quality.passed || !approval.approved);
  const counts = targetLoad.counts || {};
  const schedulerCounts = targetLoad.scheduler_counts || {};
  const scheduler = schedulerComposition(schedulerCounts);
  const sourceStatus = targetLoad.status || "waiting_for_surrogate_gate";
  const rawStatus = governanceBlocked ? "waiting_for_surrogate_gate" : sourceStatus;
  const stale = targetLoad.stale === true;
  const labels = {
    waiting_for_surrogate_gate: "R² gate 대기",
    waiting_for_optimization: "Pareto 대기",
    root_frozen: "root 확정",
    running: "FEA 실행 중",
    complete: "최종 검증 완료",
    failed: "검증 실패",
  };
  const gate = byId("targetLoadGate");
  gate.textContent = targetLoad.integrity_status === "invalid"
    ? "진행 파일 오류"
    : sourceStatus === "failed"
      ? "검증 실패"
    : governanceBlocked
      ? "v4 governance gate 대기"
      : stale
      ? `갱신 지연 · ${labels[rawStatus] || "상태 확인"}`
      : labels[rawStatus] || "상태 확인";
  gate.className = `health-pill ${!governanceBlocked && rawStatus === "complete" ? "complete" : sourceStatus === "failed" || targetLoad.integrity_status === "invalid" ? "failed" : "warning"}`;

  const candidatesTotal = integer(counts.candidates_total);
  const candidatesFinalized = integer(counts.candidates_finalized);
  const probesTotal = integer(counts.probes_total);
  const probesMatched = integer(counts.probes_matched);
  setText("targetLoadCandidateCount", `${candidatesFinalized} / ${candidatesTotal}`);
  setText("targetLoadProbeCount", `${probesMatched} / ${probesTotal}`);
  setText("targetLoadAttemptCount", `${integer(counts.attempts_issued)} 발행 · ${integer(counts.attempts_active)} 활성`);
  setText("targetLoadMtpaCount", `${integer(counts.fixed_mtpa_validated)} 검증`);
  setText("targetLoadScheduler", `Scheduler 활성 ${scheduler.active} · 완료 ${scheduler.completed} · 실패 ${scheduler.failed}`);
  const progress = byId("targetLoadProgress");
  progress.max = Math.max(1, probesTotal || candidatesTotal);
  progress.value = governanceBlocked ? 0 : probesTotal ? Math.min(probesTotal, probesMatched) : Math.min(candidatesTotal, candidatesFinalized);

  const current = targetLoad.current_probe;
  const stalePrefix = stale ? "갱신 지연 · " : "";
  if (governanceBlocked) {
    setText("targetLoadCurrent", "v4 공식 R² authority와 authorization 이후 progress root를 권위 있게 표시합니다.");
  } else if (current && current.candidate_id) {
    setText(
      "targetLoadCurrent",
      `${stalePrefix}${current.candidate_id} · ${current.operating_point_id || "운전점"} · ${current.beta_validation_role || "β"} · attempt ${integer(current.attempt_index)}`,
    );
  } else if (targetLoad.available) {
    setText("targetLoadCurrent", `v4 ${targetLoad.workflow_revision || ""} · ${stale ? "갱신 지연" : "진행 파일 검증됨"}`);
  } else {
    setText("targetLoadCurrent", "R² gate·Pareto·속도 검증 후 v4 progress root를 생성합니다.");
  }
  byId("targetLoadCurrent").classList.toggle("stale", stale);

  const failure = targetLoad.failure && typeof targetLoad.failure === "object" ? targetLoad.failure : null;
  const failureNode = byId("targetLoadFailure");
  if (failure && (failure.code || failure.message)) {
    failureNode.hidden = false;
    failureNode.textContent = `실패${failure.code ? ` ${failure.code}` : ""}${failure.message ? ` · ${failure.message}` : ""}`;
  } else {
    failureNode.hidden = true;
    failureNode.textContent = "";
  }

  const container = byId("targetLoadCandidates");
  empty(container);
  const allCandidates = Array.isArray(targetLoad.candidate_summaries)
    ? targetLoad.candidate_summaries
    : [];
  const visibleCandidates = allCandidates.slice(0, 5);
  visibleCandidates.forEach((candidate) => {
    const row = element("div", "target-load-candidate");
    row.appendChild(element("strong", "", candidate.candidate_id || "candidate"));
    const volumeCm3 = finite(candidate.objective_active_volume_m3)
      ? candidate.objective_active_volume_m3 * 1e6
      : null;
    row.appendChild(element("span", "", finite(volumeCm3) ? `${volumeCm3.toFixed(1)} cm³` : "부피 —"));
    row.appendChild(element("span", "", finite(candidate.objective_cycle_efficiency)
      ? `${(candidate.objective_cycle_efficiency * 100).toFixed(2)}%`
      : candidate.status || "대기"));
    container.appendChild(row);
  });
  const candidateOverflow = Math.max(0, allCandidates.length - visibleCandidates.length, candidatesFinalized - visibleCandidates.length);
  if (candidateOverflow) {
    container.appendChild(element("div", "target-load-overflow", `+${candidateOverflow} 후보 더 있음`));
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
  const quality = qualityGateState(data);
  const approval = authorizationGateState(data);
  const governanceBlocked = approval.v4Active && (!quality.passed || !approval.approved);
  const speedCounts = speed.scheduler_counts || {};
  const expected = integer(speed.expected_rows, 24);
  const verified = governanceBlocked ? 0 : speed.complete ? expected : Math.min(expected, integer(speed.result_rows));
  const schedulerCompleted = governanceBlocked ? 0 : Math.min(expected, integer(speedCounts.completed));
  const progressValue = Math.max(verified, schedulerCompleted);
  const active = integer(speedCounts.running) + integer(speedCounts.queued) + integer(speedCounts.attaching);
  const progress = byId("speedProgress");
  progress.max = expected;
  progress.value = progressValue;
  setText("speedRows", governanceBlocked
    ? `0 / ${expected} · governance gate 대기`
    : speed.complete
    ? `${verified} / ${expected} 검증 완료`
    : `${verified} 검증 · ${schedulerCompleted} / ${expected} scheduler 완료`);
  setText("speedStatus", governanceBlocked
    ? "v4 최적화 authorization 대기"
    : speed.complete
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
    const rawName = task.name || "—";
    const affinityPrefix = "ipmsm-v2-affinityfix-exclusive-seq-v2-";
    const cleanName = rawName.startsWith(affinityPrefix)
      ? rawName.includes("time_138_p12_baseline")
        ? "CPU affinity 단독 baseline"
        : rawName.includes("time_135_p12_iron525")
          ? "CPU affinity 단독 candidate"
          : "CPU affinity 단독 replay"
      : rawName
        .replace("ipmsm-v2-profile-thirdpass-speed-v1-", "Quality A/B · ")
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
  const progressVerified = data.campaign?.result_progress_freshness_verified === true;
  const progressObserved = progressVerified
    ? `${localTime(data.campaign?.result_progress_observed_at)} (${progressAge(data.campaign?.result_progress_age_seconds)})`
    : "확인 대기";
  setText("updatedAt", `상태 관측 ${localTime(observed)} · 결과 증가 ${progressObserved} · API ${localTime(data.generated_at)}`);
  setText("footerSchema", data.schema_version || "IPMSM dashboard");
  const liveBadge = byId("liveBadge");
  const snapshotStale = timestampAgeMs(data.generated_at) > SNAPSHOT_STALE_MS;
  const serverDegraded = data.health === "degraded";
  const schedulerOffline = data.scheduler?.reachable === false;
  const offline = !paused && schedulerOffline;
  liveBadge.classList.toggle("offline", offline);
  liveBadge.classList.toggle("stale", paused || (!offline && (snapshotStale || (serverDegraded && Boolean(data.scheduler?.reachable)))));
  setText("liveLabel", paused ? "PAUSED" : schedulerOffline ? snapshotStale ? "OFFLINE · STALE" : "OFFLINE" : snapshotStale ? "STALE" : serverDegraded ? "STALE" : "LIVE");
  renderCampaign(data);
  renderOverview(data);
  renderPipeline(data);
  renderAlerts(data);
  if (snapshotStale) {
    byId("alerts").prepend(element("div", "alert error", "대시보드 수집 시각이 30초 이상 갱신되지 않았습니다. 서버 health를 확인하세요."));
  }
  renderScheduler(data);
  renderQualityProfileExperiment(data);
  renderCheckpoint(data);
  renderModel(data);
  renderPhysics(data);
  renderTargetLoad(data);
  renderProcesses(data);
  renderTasks(data);
}

async function refresh() {
  if (polling) return;
  polling = true;
  byId("refreshButton").disabled = true;
  setText("refreshButton", REFRESH_LOADING_LABEL);
  const controller = new AbortController();
  const requestTimeout = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch("/api/status", {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    byId("liveBadge").classList.toggle("offline", !paused);
    byId("liveBadge").classList.toggle("stale", paused);
    setText("liveLabel", paused ? "PAUSED" : "OFFLINE");
    setText("updatedAt", "대시보드 연결 재시도 중");
    const container = byId("alerts");
    empty(container);
    container.appendChild(element("div", "alert error", "대시보드 상태 API에 연결할 수 없습니다. 자동으로 다시 시도합니다."));
  } finally {
    clearTimeout(requestTimeout);
    polling = false;
    byId("refreshButton").disabled = false;
    setText("refreshButton", REFRESH_LABEL);
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
  byId("liveBadge").classList.toggle("stale", paused);
  byId("liveBadge").classList.remove("offline");
  setText("liveLabel", paused ? "PAUSED" : "LIVE");
  schedule();
  if (!paused) refresh();
});
document.addEventListener("visibilitychange", () => {
  if (!paused && document.visibilityState === "visible") refresh();
});
window.addEventListener("online", () => {
  if (!paused) refresh();
});

refresh();
schedule();
