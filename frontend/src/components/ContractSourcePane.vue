<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { renderAsync } from "docx-preview";

import { getContractFile } from "../api/client";
import type { SourceClause } from "../types/review";
import DocxSourceViewer from "./DocxSourceViewer.vue";
import PdfSourceViewer from "./PdfSourceViewer.vue";

const props = defineProps<{
  clauses: SourceClause[];
  highlightedClauseId: string;
  flaggedClauseIds?: string[];
  insufficientClauseIds?: string[];
  jobId?: string;
}>();

const view = ref<"original" | "clauses">("original");
const loadingFile = ref(false);
const fileError = ref("");
const iframeUrl = ref("");
let iframeBlobUrl = "";
const docxContainer = ref<HTMLDivElement | null>(null);
const sourceScroll = ref<HTMLElement | null>(null);

function isFlagged(clause: SourceClause): boolean {
  return (props.flaggedClauseIds ?? []).includes(clause.id);
}

function isInsufficient(clause: SourceClause): boolean {
  return (props.insufficientClauseIds ?? []).includes(clause.id);
}

function isPdfClause(clause: SourceClause): boolean {
  return clause.page_start != null && clause.bboxes.length > 0;
}

async function loadOriginal(jobId: string) {
  if (!jobId) return;
  loadingFile.value = true;
  fileError.value = "";
  try {
    const blob = await getContractFile(jobId);
    const type = blob.type.toLowerCase();
    if (type.includes("pdf")) {
      revokeFileUrl();
      iframeBlobUrl = URL.createObjectURL(blob);
      // Force fit-width so Chrome's PDF viewer does not restore the previous
      // zoom level after the page is refreshed.
      iframeUrl.value = `${iframeBlobUrl}#view=FitH`;
    } else if (
      type.includes("wordprocessingml") ||
      type.includes("officedocument") ||
      type.includes("docx")
    ) {
      iframeUrl.value = "";
      if (docxContainer.value) {
        docxContainer.value.innerHTML = "";
        await renderAsync(blob.arrayBuffer(), docxContainer.value);
      }
    } else {
      fileError.value = "该文件类型暂不支持在线预览";
    }
  } catch (err) {
    fileError.value = err instanceof Error ? err.message : "合同原文加载失败";
  } finally {
    loadingFile.value = false;
  }
}

function revokeFileUrl() {
  if (iframeBlobUrl) {
    URL.revokeObjectURL(iframeBlobUrl);
    iframeBlobUrl = "";
    iframeUrl.value = "";
  }
}

onMounted(() => {
  if (props.jobId) void loadOriginal(props.jobId);
});

watch(
  () => props.jobId,
  (jobId) => {
    if (jobId) void loadOriginal(jobId);
  },
);

async function scrollToClause(clauseId: string) {
  view.value = "clauses";
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await nextTick();
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => resolve()),
    );
    const container = sourceScroll.value;
    const target = container?.querySelector(
      `[data-clause-id="${clauseId}"]`,
    );
    if (
      container &&
      target &&
      container.clientHeight > 0 &&
      target.getBoundingClientRect().height > 0
    ) {
      const containerRect = container.getBoundingClientRect();
      const targetRect = target.getBoundingClientRect();
      const top =
        container.scrollTop + (targetRect.top - containerRect.top) - 24;
      container.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
      return;
    }
  }
}

watch(
  () => props.highlightedClauseId,
  (clauseId) => {
    if (clauseId) void scrollToClause(clauseId);
  },
  { immediate: true },
);

onUnmounted(revokeFileUrl);
</script>

<template>
  <section class="source-pane" aria-label="合同原文">
    <div class="pane-heading">
      <h2>合同原文</h2>
      <div class="view-switch">
        <button
          class="view-button"
          :class="{ 'is-active': view === 'original' }"
          data-source-view-original
          @click="view = 'original'"
        >
          原文
        </button>
        <button
          class="view-button"
          :class="{ 'is-active': view === 'clauses' }"
          data-source-view-clauses
          @click="view = 'clauses'"
        >
          条款
        </button>
      </div>
      <span v-if="view === 'clauses'" class="pane-count">
        {{ clauses.length }} 个条款
      </span>
    </div>

    <div v-show="view === 'original'" class="original-wrap">
      <div v-if="loadingFile" class="source-note">正在加载合同原文…</div>
      <div v-else-if="fileError" class="source-note is-error">
        {{ fileError }}
      </div>
      <iframe
        v-else-if="iframeUrl"
        class="original-frame"
        :src="iframeUrl"
        title="合同原文"
      ></iframe>
      <div v-else ref="docxContainer" class="docx-viewer"></div>
    </div>

    <div
      v-show="view === 'clauses'"
      ref="sourceScroll"
      class="source-scroll"
    >
      <article
        v-for="clause in clauses"
        :key="clause.id"
        class="clause"
        :class="{
          'has-finding': isFlagged(clause),
          'is-insufficient': isInsufficient(clause),
          'is-highlighted': clause.id === highlightedClauseId,
        }"
        :data-clause-id="clause.id"
      >
        <header class="clause-meta">
          <span v-if="clause.article_number" class="clause-article">
            {{ clause.article_number }}
          </span>
          <span v-if="clause.page_start != null" class="clause-page">
            第 {{ clause.page_start }} 页
          </span>
          <span v-if="clause.paragraph_index != null" class="clause-paragraph">
            第 {{ clause.paragraph_index }} 段
          </span>
          <span v-if="isInsufficient(clause)" class="clause-insufficient">
            依据不足
          </span>
        </header>
        <PdfSourceViewer
          v-if="isPdfClause(clause)"
          :clause="clause"
          :highlighted="clause.id === highlightedClauseId"
        />
        <DocxSourceViewer
          v-else
          :clause="clause"
          :highlighted="clause.id === highlightedClauseId"
        />
      </article>
    </div>
  </section>
</template>

<style scoped>
.source-pane {
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
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

.original-wrap {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.original-frame {
  flex: 1;
  width: 100%;
  min-height: 480px;
  border: 1px solid #d8dee9;
  border-radius: 8px;
  background: #ffffff;
}

.docx-viewer {
  flex: 1;
  min-height: 480px;
  overflow: auto;
  padding: 18px 24px;
  border: 1px solid #d8dee9;
  border-radius: 8px;
  background: #ffffff;
}

.source-note {
  padding: 14px 16px;
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  background: #f8fafc;
  color: #64748b;
  font-size: 14px;
}

.source-note.is-error {
  border-color: #fecaca;
  background: #fef2f2;
  color: #b91c1c;
}

.source-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-right: 2px;
}

.clause {
  border: 1px solid #d8dee9;
  border-radius: 8px;
  padding: 14px 16px;
  background: #ffffff;
  scroll-margin: 12px;
  transition: border-color 140ms ease, background-color 140ms ease;
}

.clause.has-finding {
  border-color: #fcd34d;
  background: #fffbeb;
}

.clause.is-insufficient {
  border-color: #f59e0b;
  border-style: dashed;
  background: #fffbeb;
}

.clause.is-highlighted {
  border-color: #0ea5e9;
  background: #f0f9ff;
}

.clause-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-bottom: 10px;
  font-size: 13px;
}

.clause-article {
  color: #0369a1;
  font-weight: 700;
}

.clause-page,
.clause-paragraph {
  color: #64748b;
}

.clause-insufficient {
  color: #92400e;
  font-weight: 700;
}
</style>
