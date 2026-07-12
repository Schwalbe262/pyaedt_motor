from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for dashboard JavaScript semantics")
class DashboardFrontendSemanticTests(unittest.TestCase):
    def test_governance_and_stale_states_are_fail_closed(self) -> None:
        app = Path(__file__).resolve().parents[1] / "dashboard" / "app.js"
        harness = r"""
const fs = require("fs");
const vm = require("vm");

const elements = new Map();
function makeElement() {
  return {
    addEventListener() {},
    appendChild() {},
    prepend() {},
    replaceChildren() {},
    setAttribute() {},
    removeAttribute() {},
    classList: { add() {}, remove() {}, toggle() {} },
    dataset: {},
    style: {},
    textContent: "",
    disabled: false,
    hidden: false,
    max: 1,
    value: 0,
  };
}
function byId(id) {
  if (!elements.has(id)) elements.set(id, makeElement());
  return elements.get(id);
}

const context = {
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  clearInterval() {},
  clearTimeout() {},
  console,
  document: {
    addEventListener() {},
    createElement: makeElement,
    getElementById: byId,
    visibilityState: "visible",
  },
  fetch: () => new Promise(() => {}),
  setInterval: () => 0,
  setTimeout: () => 0,
  window: { addEventListener() {} },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);

const stages = [
  { id: "beta", label: "Beta", status: "complete", runtime: { completed: 1, total: 1, progress_pct: 100 } },
  { id: "stage1", label: "Stage 1", status: "complete", runtime: { completed: 700, total: 700, progress_pct: 100 } },
  { id: "surrogate", label: "Surrogate", status: "complete", runtime: { completed: 9, total: 9, progress_pct: 100 } },
  { id: "stage2", label: "Stage 2", status: "skipped" },
  { id: "stage3", label: "Stage 3", status: "skipped" },
  { id: "optimization", label: "NSGA-II", status: "complete", runtime: { completed: 900, total: 900, progress_pct: 100 } },
  { id: "speed", label: "Speed", status: "waiting" },
  { id: "target_load", label: "Target load", status: "waiting" },
];
const contradiction = {
  generated_at: new Date().toISOString(),
  health: "running",
  stale: false,
  campaign: { result_ok: 700, total: 700, active: 0 },
  scheduler: { reachable: true, stale: false, active_count: 0, cap: 100 },
  pipeline: { current_stage: "speed", current_label: "Speed", stages },
  overall: {
    current_stage: "speed",
    current_label: "Speed",
    current_status: "waiting",
    resolved_stages: 6,
    total_stages: 8,
    completed: 0,
    total: 24,
    progress_pct: 0,
  },
  model: { available: true, gate_status: "passed", threshold: 0.95, passed_count: 9, metrics: [] },
  optimization: { requires_user_confirmation: false, authorization_status: "authorized" },
  governance: {
    status: "invalid",
    contract: { activated: true, status: "verified" },
    official_stage1: {
      status: "verified",
      completion_present: true,
      r2_authority: "verified",
      gate_status: "passed",
      threshold: 0.95,
      passed_count: 9,
      target_count: 9,
      min_r2: 0.96,
      avg_r2: 0.98,
    },
    confirmation: {
      status: "verified",
      declaration_status: "verified",
      confirmation_present: true,
      confirmed: true,
    },
    authorization: { status: "invalid", receipt_present: true, authorized: false },
  },
};
context.__contradiction = contradiction;
const contradictionState = vm.runInContext("overallState(__contradiction)", context);
const contradictionQuality = vm.runInContext("qualityGateState(__contradiction)", context);
const contradictionAuthorization = vm.runInContext("authorizationGateState(__contradiction)", context);

const pending = JSON.parse(JSON.stringify(contradiction));
pending.pipeline.current_stage = "optimization";
pending.overall.current_stage = "optimization";
pending.overall.current_label = "NSGA-II";
pending.overall.resolved_stages = 5;
pending.pipeline.stages[5].status = "ready";
pending.governance.status = "awaiting_authorization";
pending.governance.authorization = { status: "absent", receipt_present: false, authorized: false };
context.__pending = pending;
const pendingState = vm.runInContext("overallState(__pending)", context);
const pendingAuthorization = vm.runInContext("authorizationGateState(__pending)", context);

const staleStages = JSON.parse(JSON.stringify(stages));
staleStages[1].status = "running";
for (let index = 2; index < staleStages.length; index += 1) staleStages[index].status = "waiting";
const stale = {
  generated_at: new Date().toISOString(),
  health: "degraded",
  stale: true,
  campaign: { result_ok: 605, total: 700, active: 100, progress_pct: 86.43 },
  scheduler: { reachable: false, stale: true, active_count: 100, cap: 100 },
  pipeline: { current_stage: "stage1", current_label: "Stage 1", stages: staleStages },
  overall: {
    current_stage: "stage1",
    current_label: "Stage 1",
    current_status: "running",
    resolved_stages: 1,
    total_stages: 8,
    completed: 605,
    total: 700,
    progress_pct: 86.43,
  },
  model: { available: false, gate_status: "waiting", threshold: 0.95, metrics: [] },
  optimization: { requires_user_confirmation: true },
};
context.__stale = stale;
vm.runInContext("renderHeroOperations(__stale, overallState(__stale))", context);
vm.runInContext("renderCampaign(__stale)", context);

const qualityExperiment = {
  integrity_status: "verified",
  plan_integrity_status: "verified",
  scheduler_integrity_status: "verified",
  scheduler_trusted: true,
  history_complete: true,
  status: "running",
  official_pipeline_stage: false,
  official_speed_stage: false,
  relation_to_official_speed: "separate_from_post_pareto_speed_validation",
  expected_cases: 24,
  expected_sources: 12,
  planned: 24,
  source_count: 12,
  active: 7,
  completed: 5,
  failed: 0,
  missing: 12,
  progress_pct: 20.83,
  scheduler_status_counts: { queued: 2, attaching: 1, running: 4, completed: 5, failed: 0, cancelled: 0 },
  profiles: ["time_138_p12_baseline", "time_135_p12_iron525"],
  project_active: 100,
  project_cap: 100,
  project_open_slots: 0,
  experiment_active_share_pct: 7,
};
context.__qualityExperiment = { quality_profile_experiment: qualityExperiment };
vm.runInContext("renderQualityProfileExperiment(__qualityExperiment)", context);
const qualityRunning = {
  state: byId("qualityExperimentCard").dataset.state,
  status: byId("qualityExperimentStatus").textContent,
  progress: byId("qualityExperimentProgress").value,
  progressLabel: byId("qualityExperimentProgressLabel").textContent,
  active: byId("qualityExperimentActive").textContent,
  missing: byId("qualityExperimentMissing").textContent,
  scheduler: byId("qualityExperimentSchedulerCounts").textContent,
  cap: byId("qualityExperimentCap").textContent,
};
const invalidQualityExperiment = JSON.parse(JSON.stringify(qualityExperiment));
invalidQualityExperiment.integrity_status = "invalid";
invalidQualityExperiment.scheduler_integrity_status = "invalid";
invalidQualityExperiment.scheduler_trusted = false;
invalidQualityExperiment.status = "complete";
invalidQualityExperiment.completed = 24;
invalidQualityExperiment.missing = 0;
context.__invalidQualityExperiment = { quality_profile_experiment: invalidQualityExperiment };
vm.runInContext("renderQualityProfileExperiment(__invalidQualityExperiment)", context);
const qualityInvalid = {
  state: byId("qualityExperimentCard").dataset.state,
  status: byId("qualityExperimentStatus").textContent,
  progress: byId("qualityExperimentProgress").value,
  completed: byId("qualityExperimentCompleted").textContent,
  missing: byId("qualityExperimentMissing").textContent,
};

process.stdout.write(JSON.stringify({
  contradiction: {
    resolved: contradictionState.resolved,
    currentId: contradictionState.currentId,
    currentStatus: contradictionState.currentStatus,
    optimizationStatus: contradictionState.stages.find((stage) => stage.id === "optimization").status,
    qualityPassed: contradictionQuality.passed,
    qualityFailed: contradictionQuality.failed,
    authorizationApproved: contradictionAuthorization.approved,
    authorizationRejected: contradictionAuthorization.rejected,
  },
  pending: {
    currentId: pendingState.currentId,
    currentStatus: pendingState.currentStatus,
    confirmed: pendingAuthorization.confirmed,
    approved: pendingAuthorization.approved,
    rejected: pendingAuthorization.rejected,
  },
  stale: {
    readiness: byId("stage1Readiness").dataset.state,
    detail: byId("stage1ReadinessDetail").textContent,
    waitTitle: byId("heroWaitTitle").textContent,
    activeSlots: byId("activeSlots").textContent,
    slotSub: byId("slotSub").textContent,
  },
  qualityRunning,
  qualityInvalid,
}));
"""
        completed = subprocess.run(
            [str(NODE), "-e", harness, str(app)],
            check=True,
            capture_output=True,
            encoding="utf-8",
            text=True,
            timeout=10,
        )
        result = json.loads(completed.stdout)

        self.assertEqual(
            result["contradiction"],
            {
                "resolved": 5,
                "currentId": "optimization",
                "currentStatus": "failed",
                "optimizationStatus": "failed",
                "qualityPassed": True,
                "qualityFailed": False,
                "authorizationApproved": False,
                "authorizationRejected": True,
            },
        )
        self.assertEqual(
            result["pending"],
            {
                "currentId": "optimization",
                "currentStatus": "waiting",
                "confirmed": True,
                "approved": False,
                "rejected": False,
            },
        )
        self.assertNotEqual(result["stale"]["readiness"], "running")
        self.assertIn("scheduler", result["stale"]["detail"].lower())
        self.assertNotIn("수집 중", result["stale"]["waitTitle"])
        self.assertEqual(result["stale"]["activeSlots"], "—")
        self.assertIn("마지막 관측", result["stale"]["slotSub"])
        self.assertEqual(result["qualityRunning"]["state"], "running")
        self.assertEqual(result["qualityRunning"]["status"], "실행 중")
        self.assertEqual(result["qualityRunning"]["progress"], 5)
        self.assertIn("5 / 24", result["qualityRunning"]["progressLabel"])
        self.assertEqual(result["qualityRunning"]["active"], "7")
        self.assertEqual(result["qualityRunning"]["missing"], "12")
        self.assertIn("queued 2", result["qualityRunning"]["scheduler"])
        self.assertIn("Project active 100 / cap 100", result["qualityRunning"]["cap"])
        self.assertEqual(
            result["qualityInvalid"],
            {
                "state": "invalid",
                "status": "identity 검증 실패",
                "progress": 0,
                "completed": "—",
                "missing": "—",
            },
        )


if __name__ == "__main__":
    unittest.main()
