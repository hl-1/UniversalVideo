import { createApp } from 'vue'
import Root from './Root.vue'
import { initializeAuth } from '../features/accounts/auth'
import '../shared/styles/index.css'
import '../features/accounts/accounts.css'

createApp(Root).mount('#app')
void initializeAuth()
