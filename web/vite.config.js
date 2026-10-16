import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy API calls to the FastAPI backend during development.
    // Start the backend first: python backend/app.py  (runs on :8000)
    // Then: cd web && npm run dev
    proxy: {
      '/api': 'http://localhost:8000',
      '/upload': 'http://localhost:8000',
      '/ping': 'http://localhost:8000',
    },
  },
})
