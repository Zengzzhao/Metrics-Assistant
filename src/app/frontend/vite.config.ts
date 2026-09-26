import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const proxy = { '/api': { target: process.env.QA_BACKEND_URL || 'http://127.0.0.1:8000', changeOrigin: true } }
export default defineConfig({ plugins: [vue()], server: { port: 5173, strictPort: true, proxy }, preview: { proxy } })
