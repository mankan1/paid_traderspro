# 0DTE Oracle — Web Deployment Guide

Dashboard with Google login, hosted on Railway (or Vercel/any Python host).

---

## Step 1 — Google OAuth credentials

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project (or use existing)
3. **APIs & Services → OAuth consent screen**
   - User type: **External**
   - App name: `0DTE Oracle`
   - Add your email as a test user
   - Scopes: `email`, `profile`, `openid`
4. **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Web application**
   - Authorised redirect URIs — add both:
     ```
     http://localhost:8000/auth/callback
     https://YOUR-APP.up.railway.app/auth/callback
     ```
5. Copy the **Client ID** and **Client Secret** — you'll need them next

---

## Step 2 — Local setup

```bash
# Clone / copy this folder
cd oracle-web

# Install dependencies
pip install -r requirements.txt

# Copy env file and fill in your values
cp .env.example .env
nano .env          # set SECRET_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET

# Build the dashboard HTML (patches allorigins → server proxy + adds login UI)
python build.py

# Run locally
python server.py
# Open http://localhost:8000
```

Generate a SECRET_KEY:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Step 3 — Deploy to Railway

### First time
```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# Create project and deploy
railway init
railway up
```

### Set environment variables in Railway dashboard
Go to your Railway project → **Variables** tab and add:

| Variable | Value |
|---|---|
| `SECRET_KEY` | your 64-char hex string |
| `GOOGLE_CLIENT_ID` | from Google Console |
| `GOOGLE_CLIENT_SECRET` | from Google Console |
| `BASE_URL` | `https://your-app.up.railway.app` |
| `ALLOWED_EMAILS` | `you@gmail.com,other@gmail.com` (or leave empty for open access) |

### After setting variables
```bash
railway up    # redeploy with new env vars
```

Railway gives you a public URL like `https://oracle-production-abc123.up.railway.app`

---

## Step 4 — Add your Railway URL to Google OAuth

Back in Google Console → **Credentials** → your OAuth client → edit:
- Add `https://YOUR-APP.up.railway.app/auth/callback` to Authorised redirect URIs
- Save

---

## Deploy to Vercel (alternative)

Vercel doesn't support long-running Python servers natively, but works with serverless functions. Easier option: **use Railway**.

If you still want Vercel:
```bash
pip install vercel
# Add vercel.json (see below) and deploy
vercel
```

`vercel.json`:
```json
{
  "builds": [{ "src": "server.py", "use": "@vercel/python" }],
  "routes": [{ "src": "/(.*)", "dest": "server.py" }]
}
```
Note: Vercel's serverless has a 10s timeout — fine for most requests but very long YF fetches may timeout.

---

## Updating the dashboard

When you get a new `oracle_yf_dashboard.html`:
```bash
cp ~/Downloads/oracle_yf_dashboard.html .
python build.py    # re-patch
railway up         # redeploy
```

---

## Access control

**Allow any Google account** (anyone with a Google login can access):
```
ALLOWED_EMAILS=          # leave empty
```

**Restrict to specific people:**
```
ALLOWED_EMAILS=you@gmail.com,trader@gmail.com,partner@company.com
```

---

## File structure

```
oracle-web/
├── server.py                    # FastAPI app (auth + YF proxy)
├── build.py                     # Patches dashboard HTML for server deploy
├── oracle_yf_dashboard.html     # Your dashboard (source — not served directly)
├── templates/
│   └── dashboard.html           # Built output (gitignored, created by build.py)
├── static/                      # Optional: any extra static assets
├── requirements.txt
├── Procfile                     # Railway/Heroku start command
├── railway.toml                 # Railway config
├── .env.example                 # Copy to .env for local dev
└── .gitignore
```

---

## How it works

```
Browser → GET /              → server.py checks session cookie
              ↓ not logged in
          GET /login         → redirect to Google
          Google consent     → POST code to /auth/callback
          /auth/callback     → exchange code, get email, set cookie
              ↓ logged in
          GET /              → serve dashboard HTML
          GET /api/yf?...    → server fetches Yahoo Finance (no CORS)
          GET /api/internals → server fetches QQQ/IWM/VIX/etc
          GET /me            → return {email, name, picture}
          GET /logout        → clear cookie → redirect to /login
```

No database. Sessions live in signed cookies (expire after 7 days).
