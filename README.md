<div align="center">

<img src="vscode-extension/icon.png" width="128" alt="BugLens Logo"/>

# BugLens

### AI-powered code review, right inside VS Code

[![Version](https://img.shields.io/badge/version-0.6.0-6366f1?style=flat-square)](https://marketplace.visualstudio.com/items?itemName=VanshDev.buglens)
[![Marketplace](https://img.shields.io/visual-studio-marketplace/i/VanshDev.buglens?style=flat-square&label=installs&color=22c55e)](https://marketplace.visualstudio.com/items?itemName=VanshDev.buglens)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791?style=flat-square&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-Upstash-DC382D?style=flat-square&logo=redis&logoColor=white)](https://upstash.com)
[![Live](https://img.shields.io/badge/API-Live%20on%20Render-46E3B7?style=flat-square)](https://buglens-api.onrender.com/docs)

**Catch bugs · Fix security flaws · Improve code quality — before it ships**

[🔌 Install Extension](https://marketplace.visualstudio.com/items?itemName=VanshDev.buglens) · [📖 API Docs](https://buglens-api.onrender.com/docs) · [🐛 Report Bug](https://github.com/Vansh940/buglens/issues)

</div>

---

## 📌 What is BugLens?

BugLens is a **VS Code extension** that reviews your code using AI and gives you instant, actionable feedback — directly inside your editor. No copy-pasting into ChatGPT. No leaving VS Code. Just press `Ctrl+Shift+R` and get a full code review in seconds.

It also works as a **GitHub PR Bot** — automatically reviewing every Pull Request and posting structured comments before any human reviewer sees the code.

---

## ✨ Features

- 🔴 **Bug Detection** — logic errors, null pointer risks, off-by-one mistakes
- 🔐 **Security Scanning** — SQL injection, XSS, hardcoded secrets, path traversal
- ⚡ **Performance Analysis** — N+1 queries, blocking I/O, memory leaks
- 🎨 **Style Violations** — naming conventions, dead code, duplication
- 📍 **Inline Highlights** — problem lines underlined red/yellow/purple directly in editor
- 💬 **Hover Tooltips** — hover any highlighted line to see issue + fix instantly
- 📊 **Score Card** — 0–100 quality score with label (Excellent / Good / Needs Work)
- 📋 **Copy Fix** — one-click copy of the exact fix suggestion
- 🔗 **Jump to Line** — click any issue to jump directly to that line
- 🤖 **GitHub PR Bot** — auto-reviews every pull request with zero human involvement
- 📈 **Status Bar** — live score shown in VS Code bottom bar after every review
- 💾 **Smart Caching** — Redis cache gives ~35% cache hit rate, repeat reviews in <50ms
- 🌐 **30+ Languages** — Python, JS, TS, Java, Go, Rust, C/C++, Swift, Dart, SQL and more

---

## 🚀 Install in 30 Seconds

### VS Code Marketplace

1. Open VS Code
2. Press `Ctrl+Shift+X`
3. Search **"BugLens"**
4. Click **Install**
5. Press `Ctrl+Shift+R` on any code file

**Or install directly:**
```bash
code --install-extension VanshDev.buglens
```

---

## 🎬 Demo

Open any file and press `Ctrl+Shift+R`:

**Input code:**
```python
def get_user(id):
    query = f"SELECT * FROM users WHERE id = {id}"
    return db.execute(query)
```

**BugLens output:**

```
🔍 BugLens Review — users.py

  35 / 100    CRITICAL

┌──────────────────────────────────────────────────────────┐
│ 🔴 CRITICAL — SQL Injection via f-string                 │
│ 📍 Line 2  (click to jump)                               │
│                                                          │
│ User input is directly interpolated into the SQL query,  │
│ allowing attackers to manipulate or destroy your database│
│                                                          │
│ Fix  Use parameterized queries:                          │
│      db.execute("SELECT * FROM users                     │
│      WHERE id = $1", [id])              [Copy Fix]       │
└──────────────────────────────────────────────────────────┘

✨ What's Good
  • Function is concise and focused on a single task
```

Line 2 is also **underlined red directly in the editor** — hover to see the issue without opening the panel.

---

## ⌨️ How to Use

| Action | How |
|---|---|
| Review current file | `Ctrl+Shift+R` / `Cmd+Shift+R` |
| Review selected code | Select code → `Ctrl+Shift+R` |
| Click toolbar button | 🔍 icon in top-right editor bar |
| Right-click menu | Right-click → **BugLens: Review This Code** |
| Status bar | Click **🐛 BugLens** at bottom right |

---

## ⚙️ Settings

Open VS Code Settings (`Ctrl+,`) → search **"buglens"**:

| Setting | Default | Description |
|---|---|---|
| `buglens.apiUrl` | `https://buglens-api.onrender.com` | Backend server URL |
| `buglens.reviewOnSave` | `false` | Auto-review on every file save |
| `buglens.showInlineHighlights` | `true` | Colored underlines on problem lines |

---

## 🌐 Supported Languages

Python · JavaScript · TypeScript · Java · Go · Rust · C · C++ · C# · Ruby · PHP · Swift · Dart · Kotlin · Scala · HTML · CSS · SCSS · SQL · Bash · YAML · JSON · Elixir · Haskell · Clojure · R · Julia · Lua · and more

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                            Clients                               │
│                                                                  │
│  ┌──────────────────┐  ┌─────────────────┐  ┌────────────────┐  │
│  │  VS Code         │  │  GitHub PR Bot  │  │  REST API      │  │
│  │  Extension       │  │  (Webhook)      │  │  /docs         │  │
│  │  Ctrl+Shift+R    │  │  Auto on PR     │  │  Swagger UI    │  │
│  └────────┬─────────┘  └───────┬─────────┘  └───────┬────────┘  │
└───────────┼────────────────────┼────────────────────┼───────────┘
            └────────────────────▼────────────────────┘
                                 │
               ┌─────────────────▼──────────────────┐
               │         FastAPI Backend             │
               │         (Render — Live 24/7)        │
               │                                     │
               │  POST /api/v1/review                │
               │  POST /api/v1/webhook               │
               │  GET  /api/v1/history               │
               │  GET  /api/v1/stats                 │
               │  GET  /health                       │
               │                                     │
               │  ┌─────────────┐ ┌───────────────┐  │
               │  │    Redis    │ │  Async Queue  │  │
               │  │   (Upstash) │ │               │  │
               │  │  24h cache  │ │               │  │
               │  └─────────────┘ └───────────────┘  │
               └──────────┬──────────────────────────┘
                          │
          ┌───────────────┼────────────────┐
          │               │                │
          ▼               ▼                ▼
  ┌──────────────┐ ┌─────────────┐ ┌─────────────┐
  │   Groq AI    │ │  PostgreSQL │ │ GitHub API  │
  │Llama 3.3 70B │ │  (Render)   │ │ PR Comments │
  └──────────────┘ └─────────────┘ └─────────────┘
```

---

## 📁 Project Structure

```
buglens/
├── backend/
│   ├── main.py                   # FastAPI entry point
│   ├── requirements.txt          # Python dependencies
│   ├── .env.example              # Environment variables template
│   ├── routers/
│   │   ├── review.py             # POST /api/v1/review
│   │   ├── webhook.py            # POST /api/v1/webhook (GitHub PR bot)
│   │   └── history.py            # GET  /api/v1/history & stats
│   ├── services/
│   │   ├── groq_service.py       # AI model integration 
│   │   ├── github_service.py     # GitHub API — fetch files, post comments
│   │   └── cache_service.py      # Redis caching layer (Upstash)
│   ├── models/
│   │   ├── database.py           # SQLAlchemy async + PostgreSQL
│   │   └── schemas.py            # Pydantic request/response models
│   └── prompts/
│       └── review_prompt.py      # System + user prompt templates
│
└── vscode-extension/
    ├── src/
    │   └── extension.ts          # Full extension — UI, highlights, status bar
    ├── icon.png                  # BugLens icon
    ├── package.json              # Extension manifest
    └── tsconfig.json
```

---

## 🔧 Self Hosting

### Prerequisites
- Python 3.12+
- PostgreSQL 16
- Redis (or Upstash free tier)
- Node.js 18+

### 1. Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/buglens.git
cd buglens/backend

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # Mac/Linux

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:
```env
GROQ=your_groq_api_key_here
GITHUB_TOKEN=your_github_personal_access_token
GITHUB_WEBHOOK_SECRET=any_random_secret_string
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/buglens
REDIS_URL=redis://localhost:6379
```

### 3. Create database

```sql
CREATE DATABASE buglens;
```

### 4. Start Redis

```bash
docker run -d -p 6379:6379 --name redis redis:7-alpine
```

### 5. Run the server

```bash
uvicorn main:app --reload --port 8000
```

Visit **http://localhost:8000/docs** ✅

### 6. Install extension locally

```bash
cd ../vscode-extension
npm install
npm run compile
npx vsce package
code --install-extension buglens-0.2.0.vsix
```

---

## 🐙 GitHub PR Bot

Automatically reviews every Pull Request — zero human involvement needed:

1. Deploy backend (see [Deploy to Render](#️-deploy-to-render))
2. Go to your GitHub repo → **Settings → Webhooks → Add webhook**
3. Fill in:
   - Payload URL: `https://buglens-api.onrender.com/api/v1/webhook`
   - Content type: `application/json`
   - Secret: same as `GITHUB_WEBHOOK_SECRET`
   - Events: **Pull requests** only
4. Save

Every PR now gets an automatic AI review comment like:

```
🔍 BugLens Review: auth/login.py
Score: 62/100 | Issues: 3

🔴 [CRITICAL] SQL Injection — Line 14
🟡 [WARNING] Hardcoded token expiry — Line 8
🔵 [SUGGESTION] Missing type hints — Line 1
```

---

## ☁️ Deploy to Render

### Backend (free — runs 24/7)

1. Push code to GitHub
2. **render.com** → New → Web Service → connect repo
3. Settings:

| Field | Value |
|---|---|
| Root Directory | `backend` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |

4. Add environment variables
5. Create free **PostgreSQL** on Render
6. Create free **Redis** on [Upstash](https://upstash.com)
7. Deploy

### Keep server awake (free)

Add your health endpoint to [UptimeRobot](https://uptimerobot.com):
- URL: `https://buglens-api.onrender.com/health`
- Interval: 5 minutes

---

## 📊 API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/v1/review` | Submit code for AI review |
| `POST` | `/api/v1/webhook` | GitHub webhook receiver |
| `GET` | `/api/v1/history` | Review history |
| `GET` | `/api/v1/stats` | Analytics and stats |
| `GET` | `/health` | Health check |

**Interactive docs:** [buglens-api.onrender.com/docs](https://buglens-api.onrender.com/docs)

### Example

```bash
curl -X POST https://buglens-api.onrender.com/api/v1/review \
  -H "Content-Type: application/json" \
  -d '{
    "code": "def divide(a, b):\n    return a / b",
    "language": "python",
    "filename": "math.py"
  }'
```

```json
{
  "summary": "Simple function missing division by zero protection.",
  "score": 52,
  "issues": [{
    "severity": "critical",
    "category": "bug",
    "line_number": 2,
    "title": "Division by zero not handled",
    "description": "If b is 0, this raises ZeroDivisionError and crashes.",
    "fix": "if b == 0: raise ValueError('Divisor cannot be zero')\nreturn a / b"
  }],
  "positive_aspects": ["Concise and readable"],
  "model_used": "Llama 3.3 70B"
}
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| AI Model | Groq API — Llama 3.3 70B |
| Backend | FastAPI + Python 3.12 |
| Database | PostgreSQL 16 + SQLAlchemy async |
| Cache | Redis (Upstash free tier) |
| GitHub Integration | Webhooks + GitHub REST API |
| VS Code Extension | TypeScript + VS Code API |
| Deployment | Render (free tier) |

---

## ❓ FAQ

**Q: Is it free to use?**
A: Yes — the hosted extension connects to a free backend. No signup, no credit card.

**Q: Does my code get stored?**
A: No — only review metadata (score, issue count, language) is stored. Your actual code is never persisted.

**Q: How accurate are the reviews?**
A: Very accurate for common issues like SQL injection, null pointers, and XSS. Treat it as a smart first pass — not a replacement for human code review.

**Q: Can I self-host the backend?**
A: Yes — see the [Self Hosting](#-self-hosting) section.

**Q: What if the backend is slow to respond?**
A: The free Render tier sleeps after 15 minutes of inactivity. First request may take ~30 seconds to wake up. UptimeRobot prevents this by pinging every 5 minutes.

**Q: Can I point the extension to my own backend?**
A: Yes — VS Code Settings → search "buglens" → change `apiUrl` to your server URL.

---

## 🤝 Contributing

1. Fork the repo
2. Create branch: `git checkout -b feature/your-feature`
3. Commit: `git commit -m "Add your feature"`
4. Push: `git push origin feature/your-feature`
5. Open a Pull Request

The BugLens GitHub bot will **automatically review your PR**. 🤖

---


<div align="center">

Built with ❤️ by **[Vansh](https://github.com/Vansh940)**

⭐ Star this repo if BugLens helped you write better code!

[![VS Code Marketplace](https://img.shields.io/badge/Install%20on-VS%20Code-007ACC?style=for-the-badge&logo=visualstudiocode)](https://marketplace.visualstudio.com/items?itemName=VanshDev.buglens)

</div>