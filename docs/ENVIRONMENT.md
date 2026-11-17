# Secrets and variables to add in GitHub

The CI/CD workflow (`.github/workflows/ci-cd.yml`) reads its configuration from your repository settings.
This page lists **everything you need to add**, where to add it, and how to get each value.
For the wider setup (EC2, Cloudflare, Vercel), see [DEPLOYMENT.md](DEPLOYMENT.md).

## Where to add them

1. Open the repo on GitHub → **Settings** (top tab).
2. Left sidebar → **Secrets and variables → Actions**.
3. There are two tabs:
   - **Secrets**: encrypted and hidden after saving. Use **New repository secret**.
   - **Variables**: plain text, visible. Use **New repository variable**.

> You can also add them under **Settings → Environments → production** (the workflow's deploy jobs use the
> `production` environment). Repository-level is simplest; either works. Don't add the same name in both with
> different values: the environment value wins.

Names are **case-sensitive** and must match exactly.

---

## Checklist

### Secrets (Secrets tab)

| # | Name | Required | What it is |
|---|---|---|---|
| 1 | `EC2_HOST` | yes | Public IP or DNS of your EC2 instance |
| 2 | `EC2_USER` | yes | SSH user on the instance |
| 3 | `EC2_SSH_KEY` | yes | Private SSH key for that user |
| 4 | `EC2_KNOWN_HOSTS` | recommended | The instance's SSH host key (protects against impersonation) |
| 5 | `TUNNEL_TOKEN` | yes | Cloudflare Tunnel token |
| 6 | `BACKEND_ENV` | yes* | The whole backend `.env` file (API keys, admin login, settings) |
| 7 | `VERCEL_TOKEN` | yes | Vercel access token |
| 8 | `VERCEL_ORG_ID` | yes | Vercel team/user id |
| 9 | `VERCEL_PROJECT_ID` | yes | Vercel project id |

\* If `BACKEND_ENV` is missing, the deploy keeps the `/opt/researchx/backend.env` that's already on the server. The
first deploy fails without one of the two.

You do **not** add `GITHUB_TOKEN`; GitHub provides it automatically (it's used to push and pull the Docker image).

### Variables (Variables tab)

| # | Name | Required | Example |
|---|---|---|---|
| 1 | `BACKEND_PUBLIC_URL` | yes | `https://api.yourdomain.com` (no trailing slash) |
| 2 | `EC2_PLATFORM` | no | `linux/arm64` for Graviton (`t4g.*`) instances. Default: `linux/amd64` |
| 3 | `VERCEL_CLI_VERSION` | no | Pin the Vercel CLI, e.g. `48.2.0`. Default: `latest` |

---

## How to get each value

### `EC2_HOST`
EC2 console → Instances → your instance → **Public IPv4 address** (e.g. `3.110.45.12`) or **Public IPv4 DNS**.
Use an **Elastic IP** so the address doesn't change when the instance restarts.

### `EC2_USER`
`ubuntu` for Ubuntu AMIs, `ec2-user` for Amazon Linux.

### `EC2_SSH_KEY`
The **private** key of the key pair you chose when launching the instance (the `.pem` you downloaded). Paste the whole
file, including the first and last lines:
```
-----BEGIN OPENSSH PRIVATE KEY-----        (or -----BEGIN RSA PRIVATE KEY-----)
...
-----END OPENSSH PRIVATE KEY-----
```
Better: make a key used only for deployments.
```bash
ssh-keygen -t ed25519 -f researchx-deploy -N ""          # creates researchx-deploy and researchx-deploy.pub
ssh -i your-launch-key.pem ubuntu@<EC2_HOST> "cat >> ~/.ssh/authorized_keys" < researchx-deploy.pub
# EC2_SSH_KEY = contents of researchx-deploy (the file WITHOUT .pub)
```

### `EC2_KNOWN_HOSTS`
Run this on your machine and paste the output:
```bash
ssh-keyscan <EC2_HOST>
```
Without it, the workflow trusts whatever host answers the first time. If you replace the instance, update this secret.

### `TUNNEL_TOKEN`
Cloudflare dashboard → **Zero Trust → Networks → Tunnels** → your tunnel → **Configure**. On the install screen, choose
**Docker** and copy the long string after `--token` (it starts with `eyJ`). Copy only the token, not the whole command.

### `BACKEND_ENV`
The full text of the backend's `.env`. It's written to the server on every deploy. Copy the template below into a
file, fill it in, and paste the **whole file** as the secret value. All the backend's settings are explained in
the next section.

```dotenv
# --- LLM providers (at least one key) ---
GROQ_API_KEY=gsk_...
GEMINI_API_KEY=...
OPENROUTER_API_KEY=sk-or-v1-...
TAVILY_API_KEY=

# --- Admin account (created on the first start) ---
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<long random password>

# --- Production ---
SECRET_KEY=<output of: openssl rand -hex 32>
CORS_ORIGINS=https://<your-project>.vercel.app
```

### `VERCEL_TOKEN`
<https://vercel.com/account/tokens> → **Create Token**. Scope it to the team that owns the project, and set an expiry you'll remember to renew.

### `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID`
From the repo root on your machine:
```bash
npm i -g vercel
vercel login
vercel link          # pick or create the project
cat .vercel/project.json
# {"orgId":"team_xxxxxxxx","projectId":"prj_xxxxxxxx"}
```
`orgId` → `VERCEL_ORG_ID`, `projectId` → `VERCEL_PROJECT_ID`. (Also in the Vercel dashboard: Project → Settings →
General → *Project ID*; Team → Settings → *Team ID*.)

### `BACKEND_PUBLIC_URL` (variable)
The public hostname you created on the Cloudflare tunnel, with `https://` and no trailing slash, e.g.
`https://api.yourdomain.com`. The frontend proxies `/api` to it, and the workflow uses it to check each deploy.

---

## Every backend setting (inside `BACKEND_ENV`)

| Variable | Default | What it does |
|---|---|---|
| `GROQ_API_KEY` | – | Groq key (<https://console.groq.com/keys>). Free tier. |
| `GEMINI_API_KEY` | – | Google AI Studio key (<https://aistudio.google.com/apikey>). Also used for embeddings. |
| `OPENROUTER_API_KEY` | – | OpenRouter key (<https://openrouter.ai/keys>), for `:free` models. Optional. |
| `OLLAMA_BASE_URL` | – | Local Ollama URL. Leave empty on EC2 unless you run Ollama there. |
| `TAVILY_API_KEY` | – | Tavily search (<https://tavily.com>). Optional; without it, free metasearch is used. |
| `MODEL_PLANNER` | `groq:openai/gpt-oss-120b,gemini:gemini-3.5-flash` | Fallback chain (`provider:model`, comma-separated) for the planner. |
| `MODEL_FAST` | `groq:openai/gpt-oss-20b,gemini:gemini-3.5-flash-lite` | Chain for quick tasks. |
| `MODEL_WRITER` | `gemini:gemini-3.5-flash,gemini:gemini-2.5-flash,groq:openai/gpt-oss-120b` | Chain for the writer and reviser. |
| `MODEL_CRITIC` | `groq:openai/gpt-oss-120b,groq:qwen/qwen3.8-27b,gemini:gemini-2.5-flash` | Chain for the critic (best kept a different family from the writer). |
| `MODEL_ANALYST` | `groq:openai/gpt-oss-120b,gemini:gemini-3.5-flash` | Chain for dataset analysis code. |
| `MODEL_JUDGE` | `groq:qwen/qwen3.8-27b,groq:openai/gpt-oss-20b,gemini:gemini-3.5-flash-lite` | Chain for evaluation (LLM-as-judge). |
| `EMBEDDING_MODEL` | `gemini-embedding-001` | Gemini embedding model for RAG. |
| `MAX_REVISIONS` | `2` | Default revision budget for new runs. |
| `PASS_SCORE` | `8.0` | Critic score (out of 10) a draft needs to pass. |
| `SEARCH_RESULTS_PER_QUERY` | `5` | Web results per search query. |
| `MAX_SOURCES` | `18` | Source cap per run (+6 per revision). |
| `ADMIN_USERNAME` | `admin` | Admin account name. |
| `ADMIN_PASSWORD` | random | Admin password, **used only when the admin is first created**. To change it later, use Account → Change password in the app; editing this value afterwards does nothing. If left empty, a random one is printed in the API log on first start. |
| `SESSION_DAYS` | `14` | How long a login lasts. |
| `GUEST_HOURS` | `24` | Guest pass length. |
| `GUESTS_PER_IP_PER_DAY` | `5` | Guest passes per visitor IP per day. |
| `SECRET_KEY` | random, stored in DB | Signs live-stream tokens. Set it so it's explicit and survives a database reset. Generate with `openssl rand -hex 32`. |
| `CORS_ORIGINS` | `*` | Sites allowed to open the live stream directly. Set it to your Vercel URL(s), comma-separated, e.g. `https://researchx.vercel.app,https://research.yourdomain.com`. |
| `COOKIE_SECURE` | `false` | **Set automatically to `true`** by `deploy/docker-compose.yml`. Don't add it. |
| `TRUST_PROXY` | `false` | **Set automatically to `true`** by the compose file (real client IPs behind Vercel/Cloudflare). Don't add it. |
| `DATA_DIR` | `backend/data` | **Set automatically to `/data`** (the Docker volume). Don't add it. |
| `APP_VERSION` | `dev` | **Set automatically** to the commit SHA when the image is built. Don't add it. |

The frontend has one build-time setting, `VITE_API_STREAM_BASE`. The workflow sets it from `BACKEND_PUBLIC_URL`, so you
don't add it anywhere.

---

## Setting them from the terminal (optional)

With the GitHub CLI (`gh auth login` first), from the repo folder:

```bash
gh secret set EC2_HOST --body "3.110.45.12"
gh secret set EC2_USER --body "ubuntu"
gh secret set EC2_SSH_KEY < researchx-deploy
ssh-keyscan 3.110.45.12 | gh secret set EC2_KNOWN_HOSTS
gh secret set TUNNEL_TOKEN --body "eyJ..."
gh secret set BACKEND_ENV < backend.prod.env          # your filled-in template (keep this file out of git)
gh secret set VERCEL_TOKEN --body "..."
gh secret set VERCEL_ORG_ID --body "team_..."
gh secret set VERCEL_PROJECT_ID --body "prj_..."
gh variable set BACKEND_PUBLIC_URL --body "https://api.yourdomain.com"

gh secret list && gh variable list                     # check (secret values are never shown)
```

Then run the pipeline: **Actions → CI/CD → Run workflow**, or push to `main`.

---

## Common mistakes

| Symptom in the Actions log | Cause |
|---|---|
| `Missing repository secrets: ...` | A name is misspelled or missing (they're case-sensitive). |
| `Missing: ... BACKEND_PUBLIC_URL` | It was added as a **secret**; it must be a **variable** (Variables tab). |
| `Permission denied (publickey)` | `EC2_SSH_KEY` is the `.pub` file, is cut short, or doesn't match `EC2_USER`. |
| `Host key verification failed` | The instance was replaced; update `EC2_KNOWN_HOSTS` (or delete it to trust on first use). |
| Tunnel check fails, API check passed | `BACKEND_PUBLIC_URL` doesn't match the tunnel's public hostname, or the hostname's service isn't `http://api:8000`. |
| Site works but the live terminal is frozen | `CORS_ORIGINS` doesn't include the exact site URL (scheme + host, no trailing slash). |
| Admin password from `BACKEND_ENV` doesn't work | The admin already existed with an older password. Sign in with that one and change it in the app. |
