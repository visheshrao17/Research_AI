#!/usr/bin/env bash
# Write frontend/vercel.json so Vercel serves the SPA and proxies /api/* to the backend's public URL.
#   deploy/render-vercel-json.sh https://api.example.com
# Proxying keeps the login cookie same-origin (no third-party cookies); the live event stream connects to the
# API host directly with a signed token (VITE_API_STREAM_BASE), so it doesn't depend on proxy streaming.
set -euo pipefail
BACKEND_URL="${1:?usage: render-vercel-json.sh https://api.example.com}"
BACKEND_URL="${BACKEND_URL%/}"
cd "$(dirname "$0")/../frontend"
cat > vercel.json <<JSON
{
  "\$schema": "https://openapi.vercel.sh/vercel.json",
  "framework": "vite",
  "installCommand": "npm ci",
  "buildCommand": "npm run build",
  "outputDirectory": "dist",
  "rewrites": [
    { "source": "/api/:path*", "destination": "${BACKEND_URL}/api/:path*" }
  ],
  "headers": [
    {
      "source": "/(.*)",
      "headers": [
        { "key": "X-Content-Type-Options", "value": "nosniff" },
        { "key": "Referrer-Policy", "value": "strict-origin-when-cross-origin" },
        { "key": "X-Frame-Options", "value": "DENY" }
      ]
    },
    {
      "source": "/assets/(.*)",
      "headers": [{ "key": "Cache-Control", "value": "public, max-age=31536000, immutable" }]
    }
  ]
}
JSON
echo "wrote frontend/vercel.json → /api proxied to ${BACKEND_URL}"
