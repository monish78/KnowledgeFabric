import { useEffect, useState } from "react";

// Placeholder until phase 9: shows backend dependency health.
export default function App() {
  const [health, setHealth] = useState(null);
  useEffect(() => {
    fetch("/api/health").then((r) => r.json()).then(setHealth).catch((e) => setHealth({ error: String(e) }));
  }, []);
  return (
    <main style={{ fontFamily: "system-ui", padding: 32 }}>
      <h1>Graphbase</h1>
      <pre>{health ? JSON.stringify(health, null, 2) : "Checking services..."}</pre>
    </main>
  );
}
