import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { keycloakCallback } from "../auth.js";

// Keycloak redirects here with ?code=...; exchange it for tokens (PKCE) and continue.
export default function AuthCallback() {
  const navigate = useNavigate();
  const [error, setError] = useState(null);
  const done = useRef(false);
  useEffect(() => {
    if (done.current) return; // StrictMode runs effects twice; the code can only be used once
    done.current = true;
    keycloakCallback(window.location.search)
      .then((next) => navigate(next, { replace: true }))
      .catch((e) => setError(e.message));
  }, [navigate]);
  return (
    <div className="page" style={{ alignItems: "center", justifyContent: "center" }}>
      {error ? (
        <div className="card" style={{ padding: 24, maxWidth: 420 }}>
          <div className="error">{error}</div>
          <div style={{ marginTop: 16 }}><Link className="btn" to="/login">Back to sign in</Link></div>
        </div>
      ) : (
        <div className="muted">Signing you in…</div>
      )}
    </div>
  );
}
