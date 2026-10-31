import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    allowedHosts: ['.trycloudflare.com'], // temporary demo links from `cloudflared tunnel`
    // FastAPI runs on :8000 (see CLAUDE.md); the browser only ever talks to /api.
    proxy: { '/api': process.env.API_TARGET ?? 'http://127.0.0.1:8000' },
  },
})
