import { createApp } from 'vue'
import { reportWebError, startDiagnostics } from '../shared/native/diagnostics'
import Root from './Root.vue'
import { initializeAuth } from '../features/accounts/auth'
import '../shared/styles/index.css'
import '../features/accounts/accounts.css'

startDiagnostics()
const app = createApp(Root)
app.config.errorHandler = (error, _instance, info) => { reportWebError(error, 'vue: ' + info); console.error(error) }
app.mount('#app')
void initializeAuth()
