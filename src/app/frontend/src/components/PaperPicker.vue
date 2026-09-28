<script setup lang="ts">
import { ref, watch } from 'vue'
import type { Paper } from '../types'
import MarkdownContent from './MarkdownContent.vue'

const props = defineProps<{ papers: Paper[]; modelValue: string; disabled: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [id: string] }>()
const open = ref(false)
const trigger = ref<HTMLButtonElement | null>(null)
watch(() => props.disabled, disabled => { if (disabled) open.value = false })
function choose(id: string) {
  emit('update:modelValue', id)
  open.value = false
  trigger.value?.focus()
}
function blur(event: FocusEvent) {
  if (!(event.currentTarget as HTMLElement).contains(event.relatedTarget as Node | null)) open.value = false
}
</script>

<template>
  <div class="paper-picker" @focusout="blur" @keydown.esc.prevent="open = false; trigger?.focus()">
    <button id="paper" ref="trigger" type="button" class="paper-trigger" :disabled="disabled"
      :aria-expanded="open" aria-controls="paper-options" aria-labelledby="paper-label" @click="open = !open">
      <MarkdownContent v-if="papers.length" :source="papers.find(p => p.id === modelValue)?.title || modelValue" />
      <span v-else>暂无论文，请先导入 Neo4j</span><span aria-hidden="true">⌄</span>
    </button>
    <div v-if="open" id="paper-options" class="paper-options">
      <button v-for="paper in papers" :key="paper.id" type="button" :aria-pressed="paper.id === modelValue" @click="choose(paper.id)">
        <MarkdownContent :source="paper.title" /><small>{{ paper.id }}</small>
      </button>
    </div>
  </div>
</template>
