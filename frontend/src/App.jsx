import { useEffect, useState } from 'react'

// Phase 0 smoke test only - proves React build -> Django static files ->
// DRF endpoint works end to end. Gets replaced/removed once Phase 1
// starts converting a real page.
export default function App() {
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/api/ping/')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(setResult)
      .catch((err) => setError(err.message))
  }, [])

  return (
    <div className="ping-card">
      <h1>React + Django Test Page</h1>
      <p>Calling <code>/api/ping/</code>...</p>
      {error && <p className="ping-error">Error: {error}</p>}
      {result && (
        <p className="ping-result">
          Response: <code>{JSON.stringify(result)}</code>
        </p>
      )}
    </div>
  )
}
