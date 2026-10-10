import { createApp } from 'vue'
import Root from './Root.vue'
import { initializeAuth } from './auth'
import './styles.css'
import './accounts.css'

createApp(Root).mount('#app')
void initializeAuth()
