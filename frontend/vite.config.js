import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: proxy /api -> backend FastAPI local (evita CORS e mantém mesma origem).
// Produção: o próprio FastAPI serve frontend/dist (ver backend/app/main.py).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
