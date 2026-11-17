# Deploying ResearchX

Backend on **AWS EC2** (Docker Compose), exposed through a **Cloudflare Tunnel**, frontend on **Vercel**, all deployed
by **GitHub Actions** (`.github/workflows/ci-cd.yml`) on every push to `main`.

```
 Browser ──► https://<app>.vercel.app ──(Vercel rewrite /api/*)──► https://api.<your-domain> ──► Cloudflare
    │                                                                                           │ tunnel (outbound
    └──── live event stream (SSE, signed token) ───────────────────► https://api.<your-domain>  │ from EC2 only)
                                                                                                 ▼
                                                          EC2: docker compose ─ cloudflared ─► api (uvicorn :8000)
                                                                                                  └ volume: /data (SQLite)
```

**Why it's wired this way**

- **Vercel proxies `/api/*` to the backend.** The browser only talks to the Vercel domain, so the login cookie is
  first-party: no CORS, no third-party-cookie blocking.
- **The live terminal's event stream goes straight to the API host.** It's authorised by a short-lived HMAC token the page
  fetches through the proxy (`GET /api/runs/{id}/stream-token`), so it doesn't depend on a proxy streaming correctly.
- **The tunnel dials out from EC2.** No inbound HTTP ports are open; the instance only needs SSH for deployments.
- **One API container, one uvicorn worker.** Running jobs and live state live in-process. Scale up the instance size, not the worker count.

## What the pipeline does

| Job | When | What |
|---|---|---|
| Backend tests | every push / PR | `pip install`, compile, `pytest backend/tests` (auth, roles, pool rules, stream tokens) |
| Frontend build | every push / PR | `npm ci && npm run build` |
| Build & push API image | push to `main` | Builds `backend/Dockerfile` → `ghcr.io/<owner>/<repo>-api:<sha>` and `:latest` |
| Deploy API to EC2 | after the image | SSH: upload `deploy/docker-compose.yml`, write `deploy.env` + `backend.env`, `docker compose pull && up -d`, wait until `/api/health` reports the new SHA locally **and** through the tunnel |
| Deploy frontend to Vercel | after the API is live | Writes `frontend/vercel.json` (rewrite to the API), builds with `VITE_API_STREAM_BASE`, `vercel deploy --prebuilt --prod` |

Pull requests run only the two CI jobs.

---

## One-time setup

You need: an AWS account, a domain on Cloudflare (free plan is fine), a Vercel account, and admin on the GitHub repo.

### 1. EC2 instance

1. Launch an instance: **Ubuntu 24.04** (or Amazon Linux 2023). Use **t3.small** (2 GB) or larger. A t3.micro works only
   with the swap the setup script adds. For Graviton (`t4g.*`), set the repo variable `EC2_PLATFORM=linux/arm64`.
2. Storage: 20 GB gp3.
3. Security group: inbound **SSH (22)** only. No HTTP/HTTPS — the tunnel handles traffic. (GitHub-hosted runners use
   many IPs; if you restrict SSH, use a self-hosted runner or AWS SSM instead.)
4. Create a key pair for deployments, or reuse one. Its **private key** becomes the `EC2_SSH_KEY` secret.
5. SSH in and run the bootstrap once:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/visheshrao17/Research_AI/main/deploy/ec2-setup.sh | bash
   exit   # log back in so your user can run docker
   ```
   It installs Docker + compose, adds 2 GB swap on small instances, and creates `/opt/researchx`.

### 2. Cloudflare Tunnel

1. Cloudflare dashboard → **Zero Trust → Networks → Tunnels → Create a tunnel** → type **Cloudflared** → name it `researchx`.
2. On the install screen, choose **Docker** and copy the long token after `--token`. This is the `TUNNEL_TOKEN` secret.
   You don't need to run the command yourself; the compose file runs cloudflared.
3. **Public Hostname** tab → add:
   - Subdomain `api`, domain `<your-domain>` → `https://api.<your-domain>`
   - Service: **HTTP** → `api:8000`. Here `api` is the compose service name; cloudflared runs in the same Docker network.
4. Save. `https://api.<your-domain>` becomes your `BACKEND_PUBLIC_URL`.

> No domain? `cloudflared tunnel --url http://api:8000` ("quick tunnel") gives a random `trycloudflare.com` URL that
> changes on every restart, which breaks the Vercel config. Use a named tunnel with a domain for anything real.

### 3. Vercel project

1. Install the CLI locally (`npm i -g vercel`) and run `vercel login`.
2. From the repo root run `vercel link`. This creates a project (e.g. `researchx`) and writes `.vercel/project.json`,
   which has `orgId` → `VERCEL_ORG_ID` and `projectId` → `VERCEL_PROJECT_ID`. Don't commit `.vercel/`; it's git-ignored.
3. Vercel dashboard → the project → **Settings → Build & Deployment**:
   - **Root Directory:** `frontend`
   - Framework preset: Vite. Build/output settings come from the generated `vercel.json`.
4. **Settings → Git:** leave the GitHub repo **disconnected**. Disconnect it if `vercel link` connected it. Deploys come
   from the workflow, which writes `vercel.json` with your API URL; Vercel's own git builds wouldn't have it.
5. Create a token at <https://vercel.com/account/tokens> → `VERCEL_TOKEN`.

### 4. GitHub secrets and variables

Repo → **Settings → Secrets and variables → Actions**. **[ENVIRONMENT.md](ENVIRONMENT.md)** walks through each one,
with where to find the value and `gh` commands to set them all from the terminal.

**Secrets**

| Name | Value |
|---|---|
| `EC2_HOST` | Public IP or DNS name of the instance |
| `EC2_USER` | `ubuntu` (Ubuntu) or `ec2-user` (Amazon Linux) |
| `EC2_SSH_KEY` | The private key (whole PEM, including the BEGIN/END lines) |
| `EC2_KNOWN_HOSTS` | *(optional, recommended)* output of `ssh-keyscan <EC2_HOST>`, to pin the host key |
| `TUNNEL_TOKEN` | The Cloudflare tunnel token |
| `BACKEND_ENV` | The full contents of the backend `.env` (template below). It's written to the server on every deploy. |
| `VERCEL_TOKEN` | Vercel access token |
| `VERCEL_ORG_ID` | From `.vercel/project.json` |
| `VERCEL_PROJECT_ID` | From `.vercel/project.json` |

**Variables**

| Name | Value |
|---|---|
| `BACKEND_PUBLIC_URL` | `https://api.<your-domain>` (no trailing slash) |
| `EC2_PLATFORM` | *(optional)* `linux/arm64` for Graviton instances; default `linux/amd64` |
| `VERCEL_CLI_VERSION` | *(optional)* pin the Vercel CLI, e.g. `48.2.0`; default `latest` |

**`BACKEND_ENV` template**

```dotenv
GROQ_API_KEY=...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=...
TAVILY_API_KEY=

ADMIN_USERNAME=admin
ADMIN_PASSWORD=<a long random password>
# Signs live-stream tokens; keep it stable across deploys (openssl rand -hex 32)
SECRET_KEY=<64 hex chars>
# Who may open the live stream directly (your Vercel URL / custom domain), comma-separated
CORS_ORIGINS=https://researchx.vercel.app

# Optional overrides (defaults shown)
# MODEL_PLANNER=groq:openai/gpt-oss-120b,gemini:gemini-3.5-flash
# MODEL_WRITER=gemini:gemini-3.5-flash,gemini:gemini-2.5-flash,groq:openai/gpt-oss-120b
# MAX_REVISIONS=2
# GUEST_HOURS=24
```

The compose file always sets `DATA_DIR=/data`, `COOKIE_SECURE=true` and `TRUST_PROXY=true`. `TRUST_PROXY` makes per-IP
limits (guest passes, login lockout) use the real client IP from `X-Forwarded-For`.

### 5. First deploy

Push to `main`, or open **Actions → CI/CD → Run workflow**. When it's green:

- `curl https://api.<your-domain>/api/health` shows `"version": "<commit sha>"`
- The job summary shows the Vercel URL. Open it → **Keeper's Office** → sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
- Optional: add a custom domain to the Vercel project. If you do, add it to `CORS_ORIGINS` too.

---

## Day-to-day

- **Deploy:** merge to `main`.
- **Roll back:** Actions → CI/CD → Run workflow → `image_tag` = the commit SHA of a good build. This skips the build
  and redeploys that image. Untick *deploy frontend* to roll back only the API.
- **Change keys or settings:** edit the `BACKEND_ENV` secret, then re-run the workflow (or edit `/opt/researchx/backend.env`
  on the server and run `docker compose --env-file deploy.env up -d`; the next deploy overwrites it with the secret if set).
- **Logs:** on the server, `cd /opt/researchx && docker compose --env-file deploy.env logs -f api` (or `cloudflared`).
- **Backups:** everything lives in the `researchx_researchx-data` volume:
  ```bash
  docker run --rm -v researchx_researchx-data:/data -v "$PWD":/backup busybox tar czf /backup/researchx-$(date +%F).tgz -C /data .
  ```
- **Restart / status:** `docker compose --env-file deploy.env ps`, `... restart api`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Deploy fails at *Check required secrets* | Add the listed secrets (step 4). |
| `Permission denied (publickey)` | `EC2_SSH_KEY` doesn't match the instance key, or `EC2_USER` is wrong. |
| `permission denied ... docker.sock` over SSH | The user isn't in the `docker` group yet: re-run `ec2-setup.sh`, or `sudo usermod -aG docker $USER`, then reconnect. |
| *Wait for the API* times out | The job prints `docker compose logs api`. Usually a bad value in `BACKEND_ENV`, or the instance ran out of memory (use a bigger instance). |
| *Verify through Cloudflare Tunnel* fails but the local check passed | The tunnel's Public Hostname must point to `http://api:8000`, and `BACKEND_PUBLIC_URL` must match that hostname. Check `docker compose logs cloudflared`. |
| Site loads but every action says the backend is unreachable / 502 | `BACKEND_PUBLIC_URL` was wrong when the frontend was built. Fix the variable and re-run the workflow. |
| Live terminal never updates in production | The stream goes directly to `BACKEND_PUBLIC_URL`. Make sure `CORS_ORIGINS` includes the exact site origin, or is `*`. |
| Everyone shares one guest-pass limit | `TRUST_PROXY` must be `true` (the compose file sets it). |
| Pulling the image fails with `denied` | The GHCR package must be linked to this repo (the workflow labels it). Check under Packages → the image → *Manage Actions access*. |

## Running the same stack by hand (no CI)

```bash
# on the server, in /opt/researchx
cp <repo>/deploy/docker-compose.yml .
printf 'API_IMAGE=ghcr.io/<owner>/<repo>-api:latest\nTUNNEL_TOKEN=<token>\n' > deploy.env
cp <your .env> backend.env
docker compose --env-file deploy.env up -d

# frontend, from the repo root on your machine
deploy/render-vercel-json.sh https://api.<your-domain>
echo "VITE_API_STREAM_BASE=https://api.<your-domain>" > frontend/.env.production.local
vercel deploy --prod
```
