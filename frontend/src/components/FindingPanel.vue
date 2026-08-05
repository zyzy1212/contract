<script setup lang="ts">
import type { Finding } from "../types/review";
import EvidenceCard from "./EvidenceCard.vue";

defineProps<{
  findings: Finding[];
  selectedFindingId: string;
  clauseLabels?: Record<string, string>;
  emptyReason?: string;
}>();

const emit = defineEmits<{ (event: "select-finding", finding: Finding): void }>();

const riskLabel = (risk: Finding["risk_level"]): string =>
  ({ high: "高风险", medium: "中风险", low: "低风险" })[risk] ?? risk;
</script>

<template>
  <aside class="finding-panel" aria-label="审核发现">
    <div class="pane-heading">
      <h2>审核发现</h2>
      <span class="pane-count">{{ findings.length }} 项</span>
    </div>
    <div class="finding-scroll">
      <div v-if="emptyReason" class="finding-empty" data-finding-empty>
        {{ emptyReason }}
      </div>
      <article
        v-for="finding in findings"
        :key="finding.id"
        class="finding-card"
        :class="[
          `risk-${finding.risk_level}`,
          { 'is-selected': finding.id === selectedFindingId },
        ]"
        :data-finding-id="finding.id"
        role="button"
        tabindex="0"
        @click="emit('select-finding', finding)"
        @keydown.enter="emit('select-finding', finding)"
      >
        <div class="finding-head">
          <span class="risk-badge">{{ riskLabel(finding.risk_level) }}</span>
          <span class="clause-ref">
            {{ clauseLabels?.[finding.clause_id] ?? finding.clause_id }}
          </span>
        </div>
        <h3 class="finding-problem">{{ finding.problem }}</h3>
        <p class="finding-reason">{{ finding.reason }}</p>
        <p class="finding-suggestion">
          <strong>建议：</strong>{{ finding.suggestion }}
        </p>
        <blockquote class="proposed-clause">{{ finding.proposed_clause }}</blockquote>
        <section class="evidence-list">
          <EvidenceCard
            v-for="evidence in finding.evidence"
            :key="evidence.id"
            :evidence="evidence"
          />
        </section>
      </article>
    </div>
  </aside>
</template>

<style scoped>
.finding-panel {
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.finding-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-right: 2px;
}

.finding-empty {
  padding: 18px 16px;
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  background: #f8fafc;
  color: #64748b;
  font-size: 14px;
  line-height: 1.7;
}

.finding-card {
  border: 1px solid #d8dee9;
  border-radius: 8px;
  padding: 14px 16px;
  background: #ffffff;
  cursor: pointer;
  outline: none;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.finding-card:hover,
.finding-card:focus-visible {
  border-color: #94a3b8;
  box-shadow: 0 2px 8px rgba(15, 23, 42, 0.08);
}

.finding-card.is-selected {
  border-color: #0369a1;
  box-shadow: inset 3px 0 0 #0369a1;
}

.finding-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.risk-badge {
  display: inline-flex;
  align-items: center;
  height: 24px;
  padding: 0 10px;
  border-radius: 999px;
  color: #ffffff;
  font-size: 12px;
  font-weight: 700;
}

.risk-high .risk-badge {
  background: #dc2626;
}

.risk-medium .risk-badge {
  background: #d97706;
}

.risk-low .risk-badge {
  background: #059669;
}

.clause-ref {
  color: #64748b;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
}

.finding-problem {
  margin: 0 0 6px;
  color: #0f172a;
  font-size: 16px;
  line-height: 1.4;
}

.finding-reason,
.finding-suggestion {
  margin: 0 0 8px;
  color: #334155;
  font-size: 14px;
  line-height: 1.65;
}

.proposed-clause {
  margin: 0 0 12px;
  padding: 10px 12px;
  border-left: 3px solid #0ea5e9;
  background: #f0f9ff;
  color: #1e293b;
  font-size: 13px;
  line-height: 1.7;
}

.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
</style>
