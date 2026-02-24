// src/main.js
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css' // 必须引入样式！
import './style.css'
import App from './App.vue'

const app = createApp(App)

app.use(ElementPlus) // 注册 Element Plus
app.mount('#app')