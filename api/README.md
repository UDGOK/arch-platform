# Architectural AI Platform

> IBC 2023 Compliant Construction Drawing Generator — Python + FastAPI + Vanilla JS

---

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI (Python 3.12) |
| Compliance Engine | Custom IBC 2023 rule set (stdlib only) |
| Frontend | Vanilla HTML/CSS/JS (no framework) |
| Deployment | Vercel (serverless) or Docker (Railway/Render) |

---

## Local Development

```bash
# 1. Clone
git clone https://github.com/YOUR_ORG/arch-platform.git
cd arch-platform

# 2. Install
pip install -r requirements.txt

# 3. Run
uvicorn api.server:app --reload --port 8000

# 4. Open
open http://localhost:8000

# 5. Unit tests
cd api && python test_compliance.py
```

API docs available at `http://localhost:8000/api/docs`

---

## Deploy to Vercel (Recommended)

### One-time setup

```bash
npm i -g vercel
vercel login
vercel link          # follow prompts, creates .vercel/project.json
```

### Add GitHub Secrets (for CI auto-deploy)

In GitHub → Settings → Secrets → Actions, add:

| Secret | Where to find it |
|---|---|
| `VERCEL_TOKEN` | vercel.com → Settings → Tokens |
| `VERCEL_ORG_ID` | `.vercel/project.json` after `vercel link` |
| `VERCEL_PROJECT_ID` | `.vercel/project.json` after `vercel link` |

### Manual deploy

```bash
vercel --prod
```

---

## Deploy to Railway / Render (Docker)

```bash
# Build image
docker build -t arch-platform .

# Run locally
docker run -p 8000:8000 arch-platform

# Push to Railway
railway up
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/meta` | All enums, jurisdiction presets |
| `POST` | `/api/validate` | Compliance check only |
| `POST` | `/api/dispatch` | Compliance + drawing generation |
| `GET` | `/api/jobs/{id}` | Retrieve job by ID |
| `GET` | `/healthz` | Health check |
| `GET` | `/api/docs` | Swagger UI |

---

## Project Structure

```
arch-platform/
├── api/
│   ├── server.py          # FastAPI app + routes
│   ├── models.py          # Domain data classes / enums
│   ├── compliance.py      # IBC 2023 rules + JurisdictionLoader
│   ├── orchestrator.py    # Engine adapters + EngineDispatcher
│   └── test_compliance.py # 31 unit tests
├── static/
│   └── index.html         # Single-page frontend
├── .github/workflows/
│   └── deploy.yml         # CI → Vercel auto-deploy
├── Dockerfile             # For Railway/Render/container
├── requirements.txt
└── vercel.json
```
