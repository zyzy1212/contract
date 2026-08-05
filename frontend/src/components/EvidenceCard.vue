<script setup lang="ts">
import type { EvidenceRef } from "../types/review";

defineProps<{ evidence: EvidenceRef }>();
</script>

<template>
  <article class="evidence-card" :data-evidence-id="evidence.id">
    <div class="evidence-head">
      <strong class="evidence-title">
        {{ evidence.source_snapshot.title }}
      </strong>
      <a
        v-if="evidence.source_snapshot.source_url"
        class="source-action"
        :href="evidence.source_snapshot.source_url"
        target="_blank"
        rel="noreferrer"
      >
        查看来源
      </a>
    </div>
    <dl class="evidence-meta">
      <div v-if="evidence.source_snapshot.section_title" class="meta-item">
        <dt>章节</dt>
        <dd>{{ evidence.source_snapshot.section_title }}</dd>
      </div>
      <div v-if="evidence.source_snapshot.article_number" class="meta-item">
        <dt>条款</dt>
        <dd>{{ evidence.source_snapshot.article_number }}</dd>
      </div>
      <div v-if="evidence.source_snapshot.page_start != null" class="meta-item">
        <dt>页码</dt>
        <dd>
          第 {{ evidence.source_snapshot.page_start }} 页
          <template
            v-if="
              evidence.source_snapshot.page_end != null &&
              evidence.source_snapshot.page_end !== evidence.source_snapshot.page_start
            "
          >
            -{{ evidence.source_snapshot.page_end }}
          </template>
        </dd>
      </div>
      <div
        v-if="evidence.source_snapshot.paragraph_index != null"
        class="meta-item"
      >
        <dt>段落</dt>
        <dd>第 {{ evidence.source_snapshot.paragraph_index }} 段</dd>
      </div>
      <div class="meta-item">
        <dt>版本</dt>
        <dd>{{ evidence.source_snapshot.version }}</dd>
      </div>
      <div v-if="evidence.source_snapshot.effective_date" class="meta-item">
        <dt>生效</dt>
        <dd>{{ evidence.source_snapshot.effective_date }}</dd>
      </div>
    </dl>
    <blockquote class="evidence-excerpt">{{ evidence.text }}</blockquote>
  </article>
</template>

<style scoped>
.evidence-card {
  border: 1px solid #d8dee9;
  border-radius: 8px;
  padding: 12px 14px;
  background: #fbfcfe;
}

.evidence-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.evidence-title {
  color: #0f172a;
  font-size: 14px;
}

.source-action {
  flex-shrink: 0;
  color: #0369a1;
  font-size: 13px;
  font-weight: 600;
  text-decoration: none;
}

.source-action:hover {
  text-decoration: underline;
}

.evidence-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin: 0 0 8px;
}

.meta-item {
  display: inline-flex;
  gap: 4px;
  margin: 0;
  font-size: 13px;
}

.meta-item dt {
  color: #64748b;
}

.meta-item dd {
  margin: 0;
  color: #1e293b;
  font-weight: 600;
}

.evidence-excerpt {
  margin: 0;
  padding-left: 12px;
  border-left: 3px solid #7dd3fc;
  color: #334155;
  font-size: 13px;
  line-height: 1.7;
}
</style>
