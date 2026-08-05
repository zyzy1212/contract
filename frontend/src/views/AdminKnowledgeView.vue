<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

import {
  activateKnowledge,
  archiveKnowledge,
  deactivateKnowledge,
  extractKnowledgeMetadata,
  listKnowledge,
  restoreKnowledge,
  uploadKnowledge,
  type KnowledgeDocument,
} from "../api/client";

const scope = ref<"public" | "firm" | "tenant_private">("public");
const sourceType = ref<"law" | "firm_rule" | "tenant_private">("law");
const view = ref<"current" | "archived">("current");
const documents = ref<KnowledgeDocument[]>([]);
const loading = ref(true);
const error = ref("");
const message = ref("");
const submitting = ref(false);
const extractingMetadata = ref(false);

const title = ref("");
const version = ref("");
const effectiveDate = ref("");
const issuingAuthority = ref("");
const sourceUrl = ref("");
const fileInput = ref<HTMLInputElement | null>(null);

const isLaw = computed(() => sourceType.value === "law");
const scopeLabel = computed(
  () =>
    ({ public: "公共法律法规库", firm: "律所专业库", tenant_private: "客户私有库" })[
      scope.value
    ],
);
const statusLabels: Record<string, string> = {
  active: "启用中",
  inactive: "已停用",
  deleted: "已归档",
};

async function loadKnowledge(silent = false) {
  if (!silent) loading.value = true;
  error.value = "";
  try {
    documents.value = await listKnowledge(
      scope.value,
      view.value === "archived" ? "deleted" : undefined,
    );
  } catch (err) {
    error.value = err instanceof Error ? err.message : "加载知识库失败";
  } finally {
    if (!silent) loading.value = false;
  }
}

onMounted(() => {
  void loadKnowledge();
});

function changeScope() {
  sourceType.value =
    scope.value === "public"
      ? "law"
      : scope.value === "firm"
        ? "firm_rule"
        : "tenant_private";
  void loadKnowledge(true);
}

function switchView(nextView: "current" | "archived") {
  if (view.value === nextView) return;
  view.value = nextView;
  void loadKnowledge(true);
}

async function waitForKnowledge(title: string, version: string) {
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    try {
      const rows = await listKnowledge(
        scope.value,
        view.value === "archived" ? "deleted" : undefined,
      );
      documents.value = rows;
      const match = rows.find(
        (row) =>
          row.title === title &&
          (!version || row.version === version) &&
          (row.status === "active" || row.status === "inactive"),
      );
      if (match) return true;
    } catch {
      // keep polling; the list endpoint may be temporarily unavailable
    }
  }
  return false;
}

function fillFromFilename(file: File) {
  const match = file.name.match(/^(.*?)(?:_(\d{8}))?\.(?:pdf|docx)$/i);
  if (!match) return;
  const base = match[1] ?? "";
  if (base && !title.value.trim()) {
    title.value = base;
  }
  if (match[2] && !version.value.trim()) {
    version.value = match[2];
  }
  if (/法|条例|规定|办法|规则|细则|决定|解释$/.test(base)) {
    sourceType.value = "law";
  }
}

async function handleFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  extractingMetadata.value = true;
  error.value = "";
  try {
    const metadata = await extractKnowledgeMetadata(file);
    if (fileInput.value?.files?.[0] !== file) return;
    if (metadata.title) title.value = metadata.title;
    if (metadata.version) version.value = metadata.version;
    if (metadata.issuing_authority) {
      issuingAuthority.value = metadata.issuing_authority;
    }
    if (metadata.effective_date) {
      effectiveDate.value = metadata.effective_date;
    }
    if (metadata.source_type === "law") {
      sourceType.value = "law";
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : "文档元数据提取失败";
  } finally {
    extractingMetadata.value = false;
  }
  fillFromFilename(file);
}

async function submitUpload(event: Event) {
  event.preventDefault();
  const selectedFile = fileInput.value?.files?.[0];
  if (!selectedFile || !title.value.trim()) return;
  const uploadedTitle = title.value.trim();
  const uploadedVersion = version.value.trim();
  submitting.value = true;
  message.value = "";
  error.value = "";
  const form = new FormData();
  form.append("file", selectedFile);
  form.append("title", title.value);
  form.append("source_type", sourceType.value);
  form.append("issuing_authority", issuingAuthority.value);
  form.append("source_url", sourceUrl.value);
  form.append("version", version.value);
  if (effectiveDate.value) {
    form.append("effective_date", effectiveDate.value);
  }
  try {
    await uploadKnowledge(form);
    message.value = "知识入库任务已提交，正在处理…";
    title.value = "";
    version.value = "";
    effectiveDate.value = "";
    issuingAuthority.value = "";
    sourceUrl.value = "";
    if (fileInput.value) fileInput.value.value = "";
    const appeared = await waitForKnowledge(uploadedTitle, uploadedVersion);
    if (!appeared) await loadKnowledge(true);
    message.value = appeared
      ? "知识入库完成"
      : "知识入库任务已提交，处理可能需要更长时间，请稍后查看";
  } catch (err) {
    error.value = err instanceof Error ? err.message : "知识上传失败";
  } finally {
    submitting.value = false;
  }
}

async function handleDeactivate(id: string) {
  try {
    await deactivateKnowledge(id);
    await loadKnowledge(true);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "停用失败";
  }
}

async function handleActivate(id: string) {
  try {
    await activateKnowledge(id);
    await loadKnowledge(true);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "启用失败";
  }
}

async function handleArchive(id: string) {
  try {
    await archiveKnowledge(id);
    await loadKnowledge(true);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "归档失败";
  }
}

async function handleRestore(id: string) {
  try {
    await restoreKnowledge(id);
    await loadKnowledge(true);
  } catch (err) {
    error.value = err instanceof Error ? err.message : "恢复失败";
  }
}
</script>

<template>
  <div class="admin-page">
    <header class="admin-header">
      <div>
        <h1>知识库管理</h1>
        <p class="scope-line">{{ scopeLabel }}</p>
      </div>
      <label class="scope-switch">
        <span>知识库层级</span>
        <select v-model="scope" data-scope-select @change="changeScope">
          <option value="public">公共</option>
          <option value="firm">律所</option>
          <option value="tenant_private">客户私有</option>
        </select>
      </label>
    </header>

    <div v-if="error" class="notice is-error">{{ error }}</div>
    <div v-if="message" class="notice">{{ message }}</div>

    <form class="upload-form" @submit="submitUpload">
      <div class="form-row">
        <label class="field">
          <span>文件</span>
          <input
            ref="fileInput"
            type="file"
            accept=".pdf,.docx"
            required
            @change="handleFileChange"
          />
        </label>
        <label class="field">
          <span>名称</span>
          <input v-model="title" data-title-input required />
        </label>
        <label class="field">
          <span>来源类型</span>
          <select v-model="sourceType" data-source-type-select>
            <option value="law">法规</option>
            <option value="firm_rule">律所规则</option>
            <option value="tenant_private">客户私有</option>
          </select>
        </label>
      </div>

      <div v-if="isLaw" class="form-row law-fields">
        <label class="field">
          <span>发布机关</span>
          <input v-model="issuingAuthority" data-authority-input required />
        </label>
        <label class="field">
          <span>权威来源网址</span>
          <input
            v-model="sourceUrl"
            data-source-url-input
            type="url"
            placeholder="https://example.test/law"
            required
          />
        </label>
      </div>

      <div class="form-row">
        <label class="field">
          <span>版本</span>
          <input v-model="version" placeholder="如 2026修订版" />
        </label>
        <label class="field">
          <span>生效日期</span>
          <input
            v-model="effectiveDate"
            data-effective-date-input
            type="date"
          />
        </label>
        <button class="submit-button" type="submit" :disabled="submitting">
          {{ submitting ? "处理中" : "入库" }}
        </button>
      </div>
    </form>

    <section class="document-section">
      <div class="pane-heading">
        <h2>知识文档</h2>
        <div class="view-switch">
          <button
            class="view-button"
            :class="{ 'is-active': view === 'current' }"
            data-view-current
            @click="switchView('current')"
          >
            当前
          </button>
          <button
            class="view-button"
            :class="{ 'is-active': view === 'archived' }"
            data-view-archived
            @click="switchView('archived')"
          >
            已归档
          </button>
        </div>
        <span class="pane-count">{{ documents.length }} 份</span>
      </div>
      <div v-if="loading" class="notice">正在加载…</div>
      <div v-else class="document-table">
        <article
          v-for="document in documents"
          :key="document.id"
          class="document-row"
        >
          <div class="document-main">
            <strong>{{ document.title }}</strong>
            <span class="document-scope">{{ document.scope }}</span>
            <span v-if="document.version" class="document-version">
              {{ document.version }}
            </span>
            <span v-if="document.effective_date" class="document-date">
              {{ document.effective_date }} 生效
            </span>
          </div>
          <span class="document-status">
            {{ statusLabels[document.status] || document.status }}
          </span>
          <button
            v-if="document.status === 'deleted'"
            class="restore-button"
            :data-restore-id="document.id"
            @click="handleRestore(document.id)"
          >
            恢复
          </button>
          <template v-else>
            <button
              v-if="document.status === 'inactive'"
              class="activate-button"
              :data-activate-id="document.id"
              @click="handleActivate(document.id)"
            >
              启用
            </button>
            <button
              v-if="document.status === 'active'"
              class="deactivate-button"
              :data-deactivate-id="document.id"
              @click="handleDeactivate(document.id)"
            >
              停用
            </button>
            <button
              class="archive-button"
              :data-archive-id="document.id"
              @click="handleArchive(document.id)"
            >
              归档
            </button>
          </template>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.admin-page {
  min-height: 100vh;
  padding: 24px;
  background: #f4f6f8;
  color: #0f172a;
  font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}

.admin-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.admin-header h1 {
  margin: 0 0 4px;
  font-size: 22px;
}

.scope-line {
  margin: 0;
  color: #64748b;
  font-size: 13px;
}

.scope-switch {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: #475569;
  font-size: 13px;
}

.scope-switch select,
.field select,
.field input {
  height: 34px;
  padding: 0 10px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  background: #ffffff;
  color: #0f172a;
}

.notice {
  margin-bottom: 14px;
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

.upload-form {
  margin-bottom: 20px;
  padding: 16px;
  border: 1px solid #d8dee9;
  border-radius: 8px;
  background: #ffffff;
}

.form-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 14px;
  margin-bottom: 12px;
}

.form-row:last-child {
  margin-bottom: 0;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 180px;
  flex: 1;
  color: #475569;
  font-size: 13px;
}

.field input,
.field select {
  width: 100%;
}

.law-fields {
  padding: 12px;
  border: 1px solid #bae6fd;
  border-radius: 8px;
  background: #f0f9ff;
}

.submit-button {
  height: 34px;
  padding: 0 18px;
  border: 1px solid #0369a1;
  border-radius: 8px;
  background: #0369a1;
  color: #ffffff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
}

.submit-button:disabled {
  opacity: 0.7;
  cursor: wait;
}

.pane-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.pane-heading h2 {
  margin: 0;
  font-size: 16px;
}

.pane-count {
  margin-left: auto;
  color: #64748b;
  font-size: 13px;
}

.view-switch {
  display: inline-flex;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  overflow: hidden;
}

.view-button {
  height: 28px;
  padding: 0 12px;
  border: 0;
  border-right: 1px solid #cbd5e1;
  background: #ffffff;
  color: #475569;
  font-size: 13px;
  cursor: pointer;
}

.view-button:last-child {
  border-right: 0;
}

.view-button.is-active {
  background: #f0f9ff;
  color: #0369a1;
  font-weight: 600;
}

.document-section {
  padding: 16px;
  border: 1px solid #d8dee9;
  border-radius: 8px;
  background: #ffffff;
}

.document-table {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.document-row {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 12px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
}

.document-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  flex: 1;
  min-width: 0;
}

.document-scope,
.document-version,
.document-date {
  color: #64748b;
  font-size: 13px;
}

.document-status {
  color: #059669;
  font-size: 13px;
  font-weight: 600;
}

.deactivate-button {
  height: 30px;
  padding: 0 12px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #f8fafc;
  color: #b91c1c;
  font-size: 13px;
  cursor: pointer;
}

.deactivate-button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.activate-button {
  height: 30px;
  padding: 0 12px;
  border: 1px solid #bbf7d0;
  border-radius: 8px;
  background: #f0fdf4;
  color: #15803d;
  font-size: 13px;
  cursor: pointer;
}

.archive-button {
  height: 30px;
  padding: 0 12px;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #f8fafc;
  color: #64748b;
  font-size: 13px;
  cursor: pointer;
}

.restore-button {
  height: 30px;
  padding: 0 12px;
  border: 1px solid #c7d2fe;
  border-radius: 8px;
  background: #eef2ff;
  color: #4338ca;
  font-size: 13px;
  cursor: pointer;
}
</style>
