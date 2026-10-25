import { onBeforeUnmount, onMounted } from 'vue'
/** Native back handling is optional; browser history is owned by each page. */
export function useAndroidBack(_handler: () => boolean, _priority = 0) {
  onMounted(() => {})
  onBeforeUnmount(() => {})
}
