<script setup lang="ts">
import { Renderer } from './markdown'
defineProps<{ source?: string | null }>()
</script>

<template>
  <div class="markdown-content"><Renderer :source="source || ''" theme="light" /></div>
</template>
