import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// In dev, proxy the API and local files to the backend (uvicorn on :8000).
// In production nginx serves dist/ and does the same routing.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/files': 'http://127.0.0.1:8000',
    },
  },
  test: { environment: 'node' },
});
