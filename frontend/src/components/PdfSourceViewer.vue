<script setup lang="ts">
import type { SourceClause } from "../types/review";


defineProps<{ clause: SourceClause; highlighted?: boolean }>();
</script>

<template>
  <div class="pdf-viewer">
    <div class="pdf-page">
      <p class="pdf-text" :class="{ 'is-highlighted': highlighted }">
        {{ clause.text }}
      </p>
    </div>
  </div>
</template>

<style scoped>
.pdf-viewer {
  width: 100%;
  min-height: 120px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #f7f8fa;
}

.pdf-page {
  position: relative;
  min-height: 120px;
  padding: 18px 20px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
}

.pdf-text {
  margin: 0;
  color: #1e293b;
  white-space: pre-wrap;
  line-height: 1.75;
  transition: background-color 140ms ease;
}

.pdf-text.is-highlighted {
  background: rgba(2, 132, 199, 0.22);
  border-radius: 4px;
  box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.16);
}
</style>
