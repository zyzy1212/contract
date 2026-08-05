<script setup lang="ts">
import {
  computed,
  onActivated,
  onDeactivated,
  onMounted,
  onUnmounted,
  ref,
} from "vue";

import {
  ApiError,
  currentActorTenant,
  getReview,
  listReviewHistory,
  rerunReview,
  uploadContract,
} from "../api/client";
import ContractSourcePane from "../components/ContractSourcePane.vue";
import FindingPanel from "../components/FindingPanel.vue";
import type {
  Finding,
  ReviewDetail,
  ReviewHistoryItem,
} from "../types/review";

const ACTIVE_JOB_KEY = "active_review_job";
const HISTORY_OPEN_KEY = "review_history_open";

const props = withDefaults(defineProps<{ jobId?: string }>(), {
  jobId: "",
});

let pollTimer: number | undefined;
const review = ref<ReviewDetail | null>(null);
const history = ref<ReviewHistoryItem[]>([]);
const historyOpen = ref(
  window.sessionStorage.getItem(HISTORY_OPEN_KEY) !== "closed",
);
const historyLoading = ref(false);
const historyError = ref("");
const loading = ref(true);
const error = ref("");
const uploadError = ref("");
const uploadState = ref<"idle" | "uploading" | "polling" | "failed">("idle");
const rerunning = ref(false);
const selectedFindingId = ref("");
const activeRisk = ref<"all" | "high" | "medium" | "low" | "insufficient">("all");
const fileInput = ref<HTMLInputElement | null>(null);

const selectedFinding = computed<Finding | undefined>(() =>
  review.value?.findings.find((finding) => finding.id === selectedFindingId.value),
);

const flaggedClauseIds = computed<string[]>(() => {
  const ids = new Set<string>();
  for (const finding of review.value?.findings ?? []) {
    if (finding.clause_id) ids.add(finding.clause_id);
  }
  return Array.from(ids);
});

const clauseLabels = computed<Record<string, string>>(() => {
  const labels: Record<string, string> = {};
  for (const clause of review.value?.source_clauses ?? []) {
    labels[clause.id] = clause.article_number || `条款 ${clause.id}`;
  }
  return labels;
});

const visibleFindings = computed<Finding[]>(() => {
  if (!review.value) return [];
  if (activeRisk.value === "insufficient") return [];
  if (activeRisk.value === "all") return review.value.findings;
  return review.value.findings.filter(
    (finding) => finding.risk_level === activeRisk.value,
  );
});

const statusLabels: Record<string, string> = {
  queued: "排队等待中",
  running: "解析审核中",
  complete: "审核完成",
  partial: "部分完成",
  failed: "审核失败",
};

const statusLabel = computed(() => {
  const status = review.value?.status ?? "";
  return statusLabels[status] || status || "等待上传";
});

const findingEmptyReason = computed(() => {
  if (!review.value) return "";
  if (!["complete", "partial", "failed"].includes(review.value.status)) return "";
  if (review.value.findings.length > 0) return "";
  const insufficient = review.value.insufficient_clause_count ?? 0;
  return insufficient > 0
    ? `${insufficient} 个条款因知识库依据不足未生成风险发现，可补充相关法规后重新审核。`
    : "知识库中未找到足够依据，系统不会发布未经验证的发现。";
});

async function loadHistory() {
  historyLoading.value = true;
  historyError.value = "";
  try {
    history.value = await listReviewHistory();
  } catch (err) {
    historyError.value =
      err instanceof Error ? err.message : "加载合同历史失败";
  } finally {
    historyLoading.value = false;
  }
}

function toggleHistory() {
  historyOpen.value = !historyOpen.value;
  window.sessionStorage.setItem(
    HISTORY_OPEN_KEY,
    historyOpen.value ? "open" : "closed",
  );
}

function syncHistoryFromDetail(detail: ReviewDetail) {
  const item = history.value.find(
    (candidate) => candidate.id === detail.id,
  );
  if (!item) return;
  item.status = detail.status;
  item.total_clauses = detail.total_clauses;
  item.completed_clauses = detail.completed_clauses;
}

async function selectHistoryItem(item: ReviewHistoryItem) {
  if (item.id === review.value?.id) return;
  stopPolling();
  rememberActiveJob(item.id);
  await loadReview(item.id);
}

async function loadReview(jobId: string) {
  loading.value = true;
  error.value = "";
  try {
    const detail = await getReview(jobId);
    review.value = detail;
    syncHistoryFromDetail(detail);
    selectedFindingId.value = detail.findings[0]?.id ?? "";
    if (detail.status === "queued" || detail.status === "running") {
      startPolling(jobId);
    }
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      window.sessionStorage.removeItem(ACTIVE_JOB_KEY);
      review.value = null;
      error.value = "";
    } else {
      error.value = err instanceof Error ? err.message : "加载审核结果失败";
    }
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void loadHistory();
  if (props.jobId) {
    void loadReview(props.jobId);
  } else {
    const storedJobId = storedJobForCurrentTenant();
    if (storedJobId) {
      void loadReview(storedJobId);
    } else {
      loading.value = false;
    }
  }
});

onUnmounted(() => {
  stopPolling();
});

onActivated(() => {
  const current = review.value;
  if (current && (current.status === "queued" || current.status === "running")) {
    startPolling(current.id);
  }
});

onDeactivated(() => {
  stopPolling();
});

async function handleFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  uploadState.value = "uploading";
  uploadError.value = "";
  try {
    const job = await uploadContract(file);
    rememberActiveJob(job.id);
    void loadHistory();
    startPolling(job.id);
  } catch (err) {
    uploadState.value = "failed";
    uploadError.value = err instanceof Error ? err.message : "上传失败";
  } finally {
    if (input) input.value = "";
  }
}

function startPolling(jobId: string) {
  stopPolling();
  uploadState.value = "polling";
  pollTimer = window.setInterval(async () => {
    try {
      const detail = await getReview(jobId);
      review.value = detail;
      syncHistoryFromDetail(detail);
      if (["complete", "partial", "failed"].includes(detail.status)) {
        stopPolling();
        uploadState.value = "idle";
        selectedFindingId.value = detail.findings[0]?.id ?? "";
      }
    } catch (err) {
      stopPolling();
      uploadState.value = "idle";
      if (err instanceof ApiError && err.status === 404) {
        window.sessionStorage.removeItem(ACTIVE_JOB_KEY);
        review.value = null;
        error.value = "";
      } else {
        error.value = err instanceof Error ? err.message : "加载审核结果失败";
      }
    }
  }, 1500);
}

function rememberActiveJob(jobId: string) {
  const stored = window.sessionStorage.getItem(ACTIVE_JOB_KEY);
  let jobs: Record<string, string> = {};
  if (stored) {
    try {
      const parsed = JSON.parse(stored);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        if ("jobId" in parsed && "tenantId" in parsed) {
          jobs = {
            [String(parsed.tenantId)]: String(parsed.jobId),
          };
        } else {
          jobs = parsed as Record<string, string>;
        }
      }
    } catch {
      jobs = {};
    }
  }
  jobs[currentActorTenant()] = jobId;
  window.sessionStorage.setItem(
    ACTIVE_JOB_KEY,
    JSON.stringify(jobs),
  );
}

function storedJobForCurrentTenant(): string {
  const stored = window.sessionStorage.getItem(ACTIVE_JOB_KEY);
  if (!stored) return "";
  try {
    const parsed = JSON.parse(stored);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return "";
    }
    if ("jobId" in parsed && "tenantId" in parsed) {
      return parsed.tenantId === currentActorTenant()
        ? String(parsed.jobId)
        : "";
    }
    const jobId = parsed[currentActorTenant()];
    return typeof jobId === "string" ? jobId : "";
  } catch {
    return "";
  }
}

function stopPolling() {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer);
    pollTimer = undefined;
  }
}

async function handleRerun() {
  const current = review.value;
  if (!current) return;
  rerunning.value = true;
  uploadError.value = "";
  try {
    const job = await rerunReview(current.id);
    rememberActiveJob(job.id);
    await loadReview(job.id);
    void loadHistory();
  } catch (err) {
    uploadState.value = "failed";
    uploadError.value = err instanceof Error ? err.message : "重新审核失败";
  } finally {
    rerunning.value = false;
  }
}

function selectFinding(finding: Finding) {
  selectedFindingId.value = finding.id;
}
</script>

<template>
  <div class="review-page">
    <header class="page-header">
      <div class="header-copy">
        <h1>合同审核</h1>
        <p class="job-line">
          任务 {{ review?.id ?? "未开始" }}
          <template v-if="review">
            · {{ statusLabel }} · 合同 {{ review.contract_id }}
          </template>
        </p>
      </div>
      <label class="upload-control">
        <input
          ref="fileInput"
          type="file"
          accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          hidden
          @change="handleFile"
        />
        <span
          class="upload-button"
          :class="{ 'is-busy': uploadState === 'uploading' || uploadState === 'polling' }"
        >
          {{ uploadState === "idle" ? "上传合同" : "处理中" }}
        </span>
      </label>
    </header>

    <div class="workspace">
      <aside
        class="history-sidebar"
        :class="{ 'is-collapsed': !historyOpen }"
      >
        <button
          class="sidebar-toggle"
          data-history-toggle
          :aria-expanded="historyOpen"
          :title="historyOpen ? '收起合同列表' : '展开合同列表'"
          @click="toggleHistory"
        >
          {{ historyOpen ? "‹" : "›" }}
        </button>
        <div v-if="historyOpen" class="history-panel">
          <div class="history-title">我的合同</div>
          <div v-if="historyLoading" class="history-note">正在加载…</div>
          <div v-else-if="historyError" class="history-note is-error">
            {{ historyError }}
          </div>
          <div v-else-if="history.length === 0" class="history-note">
            还没有上传合同
          </div>
          <ul v-else class="history-list">
            <li v-for="item in history" :key="item.id">
              <button
                class="history-item"
                :data-history-id="item.id"
                :class="{ 'is-active': item.id === review?.id }"
                @click="selectHistoryItem(item)"
              >
                <span class="history-filename" :title="item.filename">
                  {{ item.filename }}
                </span>
                <span class="history-meta">
                  {{ statusLabels[item.status] || item.status }}
                  · {{ item.completed_clauses }}/{{ item.total_clauses }}
                </span>
              </button>
            </li>
          </ul>
        </div>
      </aside>

      <main class="review-main">
        <div v-if="uploadError" class="notice is-error">{{ uploadError }}</div>
        <div v-if="error" class="notice is-error">{{ error }}</div>
        <div v-if="review?.failure_reason" class="notice is-error">
          失败原因：{{ review.failure_reason }}
        </div>
        <div v-if="loading" class="notice">正在加载审核结果…</div>

        <template v-if="review">
          <div class="progress-strip">
            <span
              v-if="review.status === 'queued'"
              class="processing-hint"
            >
              排队等待处理中…
            </span>
            <span
              v-else-if="review.status === 'running'"
              class="processing-hint"
            >
              正在解析合同并审核条款…
            </span>
            <span class="progress-copy">
              已完成 {{ review.completed_clauses }} / {{ review.total_clauses }} 个条款
            </span>
            <span v-if="review.findings.length" class="partial-findings">
              已发现 {{ review.findings.length }} 项风险
            </span>
            <span v-if="review.unreviewed_clause_ids.length" class="unreviewed">
              未审核 {{ review.unreviewed_clause_ids.length }} 项
            </span>
            <button
              v-if="['complete', 'partial', 'failed'].includes(review.status)"
              class="rerun-button"
              data-rerun-button
              :disabled="rerunning"
              @click="handleRerun"
            >
              {{ rerunning ? "重新提交中" : "重新审核" }}
            </button>
            <label class="filter-control">
              <span>风险筛选</span>
              <select v-model="activeRisk">
                <option value="all">全部</option>
                <option value="high">高</option>
                <option value="medium">中</option>
                <option value="low">低</option>
                <option value="insufficient">依据不足</option>
              </select>
            </label>
          </div>

          <div class="review-grid">
            <ContractSourcePane
              :clauses="review.source_clauses"
              :highlighted-clause-id="selectedFinding?.clause_id ?? ''"
              :flagged-clause-ids="flaggedClauseIds"
              :job-id="review.id"
            />
            <FindingPanel
              :findings="visibleFindings"
              :selected-finding-id="selectedFindingId"
              :clause-labels="clauseLabels"
              :empty-reason="findingEmptyReason"
              @select-finding="selectFinding"
            />
          </div>
        </template>

        <div v-else-if="!loading && !error" class="empty-state">
          上传 PDF 或 DOCX 合同后开始审核。
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.review-page {
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: #f4f6f8;
  color: #0f172a;
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 24px;
  background: #ffffff;
  border-bottom: 1px solid #d8dee9;
}

.workspace {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: stretch;
}

.history-sidebar {
  width: 260px;
  flex: 0 0 260px;
  display: flex;
  flex-direction: column;
  background: #ffffff;
  border-right: 1px solid #d8dee9;
  overflow: hidden;
}

.history-sidebar.is-collapsed {
  width: 44px;
  flex-basis: 44px;
}

.sidebar-toggle {
  height: 40px;
  border: none;
  background: transparent;
  color: #64748b;
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
}

.sidebar-toggle:hover {
  background: #f1f5f9;
  color: #0f172a;
}

.history-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  padding-bottom: 16px;
}

.history-title {
  padding: 12px 16px 8px;
  font-size: 14px;
  font-weight: 600;
  color: #0f172a;
}

.history-note {
  padding: 8px 16px;
  color: #64748b;
  font-size: 13px;
}

.history-note.is-error {
  color: #b91c1c;
}

.history-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.history-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  width: 100%;
  padding: 10px 16px;
  border: none;
  border-bottom: 1px solid #eef2f7;
  background: transparent;
  color: #1e293b;
  text-align: left;
  cursor: pointer;
}

.history-item:hover {
  background: #f8fafc;
}

.history-item.is-active {
  background: #eef6ff;
  border-left: 3px solid #0369a1;
}

.history-filename {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.35;
  word-break: break-all;
}

.history-meta {
  color: #64748b;
  font-size: 12px;
}

.review-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.header-copy h1 {
  margin: 0 0 4px;
  font-size: 22px;
  line-height: 1.2;
}

.job-line {
  margin: 0;
  color: #64748b;
  font-size: 13px;
}

.upload-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 112px;
  height: 36px;
  padding: 0 16px;
  border: 1px solid #0369a1;
  border-radius: 8px;
  background: #0369a1;
  color: #ffffff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}

.upload-button.is-busy {
  opacity: 0.7;
  cursor: wait;
}

.notice {
  margin: 12px 24px 0;
  padding: 10px 14px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #f8fafc;
  color: #334155;
  font-size: 14px;
}

.notice.is-error {
  border-color: #fecaca;
  background: #fef2f2;
  color: #b91c1c;
}

.progress-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px 18px;
  padding: 12px 24px;
  font-size: 14px;
}

.progress-copy {
  color: #1e293b;
  font-weight: 600;
}

.unreviewed {
  color: #b45309;
  font-size: 13px;
}

.partial-findings {
  color: #b45309;
  font-size: 13px;
  font-weight: 600;
}

.processing-hint {
  color: #0369a1;
  font-size: 13px;
  font-weight: 600;
}

.rerun-button {
  height: 30px;
  padding: 0 12px;
  border: 1px solid #c7d2fe;
  border-radius: 8px;
  background: #eef2ff;
  color: #4338ca;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}

.rerun-button:disabled {
  opacity: 0.6;
  cursor: wait;
}

.filter-control {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  color: #475569;
  font-size: 13px;
}

.filter-control select {
  height: 32px;
  padding: 0 8px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #ffffff;
  color: #0f172a;
}

.review-grid {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(380px, 0.85fr);
  grid-template-rows: minmax(0, 1fr);
  gap: 16px;
  padding: 0 24px 24px;
}

.empty-state {
  padding: 48px 24px;
  text-align: center;
  color: #64748b;
}

@media (max-width: 900px) {
  .review-page {
    height: auto;
    overflow: visible;
  }

  .workspace {
    flex-direction: column;
    align-items: stretch;
  }

  .history-sidebar,
  .history-sidebar.is-collapsed {
    width: 100%;
    flex-basis: auto;
    border-right: none;
    border-bottom: 1px solid #d8dee9;
  }

  .sidebar-toggle {
    width: 100%;
  }

  .review-main {
    overflow: visible;
  }

  .review-grid {
    grid-template-columns: 1fr;
    gap: 18px;
  }

  .source-pane,
  .finding-panel {
    min-height: 420px;
  }
}
</style>
