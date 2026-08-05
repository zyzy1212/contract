<script setup lang="ts">
import type { SourceClause } from "../types/review";

defineProps<{ clause: SourceClause; highlighted?: boolean }>();
</script>

<template>
  <p class="docx-paragraph" :class="{ 'is-highlighted': highlighted }">
    {{ clause.text }}
  </p>
</template>

<style scoped>
.docx-paragraph {
  margin: 0;
  color: #1e293b;
  line-height: 1.8;
  white-space: pre-wrap;
  transition: background-color 140ms ease;
}

.docx-paragraph.is-highlighted {
  background: rgba(2, 132, 199, 0.22);
  border-radius: 4px;
  box-shadow: 0 0 0 3px rgba(2, 132, 199, 0.16);
}
</style>
