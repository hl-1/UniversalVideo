<script setup lang="ts">
import { ref, watch } from 'vue'
import { Disc3 } from 'lucide-vue-next'
const props = defineProps<{ src?: string | null; title?: string }>()
const failed = ref(false)
watch(() => props.src, () => { failed.value = false })
</script>

<template>
  <span class="track-art">
    <img v-if="src && !failed" :src="src" :alt="title || ''" loading="lazy" @error="failed = true" />
    <Disc3 v-else :size="25" aria-hidden="true" />
  </span>
</template>
