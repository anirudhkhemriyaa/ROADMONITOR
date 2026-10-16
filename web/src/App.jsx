/**
 * Road Monitor - Web Dashboard
 *
 * This is the root React component. The web app will display drive data
 * collected by the Android app and Termux logger, fetched from the FastAPI
 * backend (backend/app.py).
 *
 * Available backend endpoints (proxy configured in vite.config.js):
 *   GET /api/drives              - list all uploaded drives
 *   GET /api/drives/:name/preview- accelerometer chart + GPS track for one drive
 *   GET /api/live                - live streaming sensor data
 *   GET /api/info                - server LAN IPs and connected phones
 *   GET /ping                    - health check
 *   POST /upload                 - upload a drive CSV
 *
 * To start developing:
 *   1. python backend/app.py          (starts FastAPI on :8000)
 *   2. cd web && npm install && npm run dev   (starts Vite on :5173)
 */
function App() {
  return (
    <div style={{ fontFamily: 'system-ui, sans-serif', padding: '2rem', maxWidth: '800px', margin: '0 auto' }}>
      <h1>Road Monitor</h1>
      <p>
        Web dashboard scaffold — ready for development.
      </p>
      <h2>Quick start</h2>
      <ol>
        <li>Start the backend: <code>python backend/app.py</code></li>
        <li>Install web dependencies: <code>cd web &amp;&amp; npm install</code></li>
        <li>Start the dev server: <code>npm run dev</code></li>
        <li>Open <a href="http://localhost:5173">http://localhost:5173</a></li>
      </ol>
      <h2>Backend API</h2>
      <ul>
        <li><code>GET /api/drives</code> — list uploaded drives</li>
        <li><code>GET /api/live</code> — live sensor stream</li>
        <li><code>GET /api/info</code> — server info</li>
        <li><code>GET /ping</code> — health check</li>
      </ul>
    </div>
  )
}

export default App
