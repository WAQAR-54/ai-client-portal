import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Builds straight into Django's static/ dir (STATICFILES_DIRS in
// config/settings.py) as static/react/main.js + main.css - fixed
// filenames (no content hash) so templates can reference them with
// this app's existing {% static_v %} tag, which already handles cache-
// busting via a ?v=<mtime> query string for every other static asset
// here. Keeps this app's one cache-busting convention instead of adding
// a second one (Vite's manifest.json) just for React-built files.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../static/react',
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: 'main.js',
        chunkFileNames: 'main-[name].js',
        assetFileNames: 'main.[ext]',
      },
    },
  },
})
