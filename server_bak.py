"""
server.py — 0DTE Oracle  ·  FastAPI + Google OAuth + Stripe + SQLite
=====================================================================
Free tier : 60-second preview (signal + chart only), then paywall
Premium   : $10/month via Stripe, full dashboard unlocked
Auth      : Google OAuth 2.0
Data      : Yahoo Finance proxy (server-side, no CORS)
Storage   : SQLite (aiosqlite) — no external DB needed
"""

import os, time, json, hmac, hashlib, asyncio, logging
from pathlib import Path
from urllib.parse import urlencode, quote
from typing import Optional
from contextlib import asynccontextmanager

import httpx, aiosqlite
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oracle")

# ── Config ──────────────────────────────────────────────────────────
SECRET_KEY            = os.environ["SECRET_KEY"]
GOOGLE_CLIENT_ID      = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET  = os.environ["GOOGLE_CLIENT_SECRET"]
STRIPE_SECRET_KEY     = os.environ["STRIPE_SECRET_KEY"]
STRIPE_PUB_KEY        = os.environ["STRIPE_PUBLISHABLE_KEY"]
STRIPE_PRICE_ID       = os.environ["STRIPE_PRICE_ID"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
BASE_URL              = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
ALLOWED_EMAILS        = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS","").split(",") if e.strip()}
DB_PATH               = os.environ.get("DB_PATH", str(Path(__file__).parent / "oracle.db"))
PREVIEW_SECONDS       = int(os.environ.get("PREVIEW_SECONDS", "60"))

STRIPE_API  = "https://api.stripe.com/v1"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOK  = "https://oauth2.googleapis.com/token"
GOOGLE_USER = "https://www.googleapis.com/oauth2/v3/userinfo"
YF_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120",
    "Accept": "application/json",
}

# ── Database ─────────────────────────────────────────────────────────
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                email              TEXT PRIMARY KEY,
                name               TEXT,
                picture            TEXT,
                stripe_customer_id TEXT,
                is_premium         INTEGER DEFAULT 0,
                subscription_id    TEXT,
                subscription_end   INTEGER,
                created_at         INTEGER DEFAULT (strftime('%s','now'))
            )""")
        await db.commit()
    log.info(f"DB ready: {DB_PATH}")

async def db_upsert(email, name, picture):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(email,name,picture) VALUES(?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET name=excluded.name, picture=excluded.picture",
            (email, name, picture))
        await db.commit()

async def db_get(email) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE email=?", (email,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def db_get_by_customer(cid) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE stripe_customer_id=?", (cid,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def db_set_customer(email, cid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET stripe_customer_id=? WHERE email=?", (cid, email))
        await db.commit()

async def db_grant_premium(email, cid, sub_id, sub_end):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET stripe_customer_id=?, subscription_id=?, "
            "is_premium=1, subscription_end=? WHERE email=?",
            (cid, sub_id, sub_end, email))
        await db.commit()

async def db_revoke_premium(email):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET is_premium=0, subscription_id=NULL WHERE email=?", (email,))
        await db.commit()

def premium_active(row) -> bool:
    if not row or not row.get("is_premium"): return False
    end = row.get("subscription_end")
    return not (end and int(end) < int(time.time()))

# ── Stripe helpers ────────────────────────────────────────────────────
async def s_post(ep, data):
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{STRIPE_API}/{ep}", data=data,
                         auth=(STRIPE_SECRET_KEY,""), timeout=15)
        r.raise_for_status(); return r.json()

async def s_get(ep):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{STRIPE_API}/{ep}", auth=(STRIPE_SECRET_KEY,""), timeout=15)
        r.raise_for_status(); return r.json()

async def ensure_customer(email, name) -> str:
    row = await db_get(email)
    if row and row.get("stripe_customer_id"): return row["stripe_customer_id"]
    c = await s_post("customers", {"email": email, "name": name})
    await db_set_customer(email, c["id"])
    return c["id"]

def stripe_sig_ok(payload: bytes, header: str) -> bool:
    try:
        parts = dict(p.split("=",1) for p in header.split(","))
        signed = parts["t"].encode() + b"." + payload
        exp = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(exp, parts["v1"])
    except Exception: return False

# ── App ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app):
    await db_init()
    try: get_html(); log.info("Dashboard HTML loaded ✓")
    except FileNotFoundError: log.warning("templates/dashboard.html missing — run build.py")
    log.info(f"Oracle ready  BASE_URL={BASE_URL}  preview={PREVIEW_SECONDS}s")
    yield

app = FastAPI(docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY,
    session_cookie="oracle_session", max_age=86400*7,
    same_site="lax", https_only=BASE_URL.startswith("https"))

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

_html: Optional[str] = None
def get_html():
    global _html
    if _html is None:
        _html = (Path(__file__).parent / "templates" / "dashboard.html").read_text("utf-8")
    return _html

def auth_session(req: Request):
    return req.session.get("user")

def require_api_auth(req: Request):
    if not auth_session(req): raise HTTPException(401, "Not authenticated")

# ── Google OAuth ──────────────────────────────────────────────────────
@app.get("/login")
async def login(req: Request):
    p = {"client_id": GOOGLE_CLIENT_ID, "redirect_uri": f"{BASE_URL}/auth/callback",
         "response_type": "code", "scope": "openid email profile",
         "access_type": "online", "prompt": "select_account",
         "state": req.query_params.get("next", "/")}
    return RedirectResponse(GOOGLE_AUTH + "?" + urlencode(p), 302)

@app.get("/auth/callback")
async def callback(req: Request, code: str = Query(...), state: str = Query("/")):
    async with httpx.AsyncClient() as c:
        tok = await c.post(GOOGLE_TOK, data={"code": code, "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET, "redirect_uri": f"{BASE_URL}/auth/callback",
            "grant_type": "authorization_code"})
        if tok.status_code != 200:
            return HTMLResponse("<h2>Login failed. <a href='/login'>Try again</a></h2>", 400)
        ui = await c.get(GOOGLE_USER, headers={"Authorization": f"Bearer {tok.json()['access_token']}"})
    if ui.status_code != 200:
        return HTMLResponse("<h2>Login failed. <a href='/login'>Try again</a></h2>", 400)
    u = ui.json()
    email, name, pic = u["email"].lower(), u.get("name", u["email"]), u.get("picture", "")
    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        return HTMLResponse(f"<h2>Access denied: {email}</h2>", 403)
    await db_upsert(email, name, pic)
    req.session["user"] = {"email": email, "name": name, "picture": pic}
    log.info(f"Login: {email}")
    return RedirectResponse(state if state.startswith("/") else "/", 302)

@app.get("/logout")
async def logout(req: Request):
    req.session.clear(); return RedirectResponse("/login", 302)

# ── /me — polled by dashboard JS ──────────────────────────────────────
@app.get("/me")
async def me(req: Request):
    s = auth_session(req)
    if not s: return JSONResponse({"authenticated": False, "premium": False})
    row = await db_get(s["email"])
    return JSONResponse({
        "authenticated":  True,
        "premium":        premium_active(row),
        "email":          s["email"],
        "name":           s.get("name", ""),
        "picture":        s.get("picture", ""),
        "stripe_pub_key": STRIPE_PUB_KEY,
        "preview_seconds": PREVIEW_SECONDS,
    })

# ── Dashboard ─────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(req: Request):
    if not auth_session(req): return RedirectResponse("/login?next=/", 302)
    return HTMLResponse(get_html())

# ── Stripe: create checkout session ──────────────────────────────────
@app.post("/stripe/checkout")
async def checkout(req: Request):
    s = auth_session(req)
    if not s: raise HTTPException(401)
    try:
        cid = await ensure_customer(s["email"], s.get("name", s["email"]))
        sess = await s_post("checkout/sessions", {
            "customer": cid,
            "mode": "subscription",
            "line_items[0][price]": STRIPE_PRICE_ID,
            "line_items[0][quantity]": "1",
            "success_url": f"{BASE_URL}/stripe/success?sid={{CHECKOUT_SESSION_ID}}",
            "cancel_url":  f"{BASE_URL}/?cancelled=1",
            "allow_promotion_codes": "true",
            "billing_address_collection": "auto",
        })
        return JSONResponse({"url": sess["url"]})
    except Exception as e:
        log.error(f"Checkout: {e}"); raise HTTPException(500, str(e))

# ── Stripe: success redirect ──────────────────────────────────────────
@app.get("/stripe/success")
async def stripe_success(req: Request, sid: str = Query(...)):
    s = auth_session(req)
    if not s: return RedirectResponse("/login", 302)
    try:
        ch = await s_get(f"checkout/sessions/{sid}")
        if ch.get("payment_status") == "paid" or ch.get("status") == "complete":
            sub_id = ch.get("subscription"); cid = ch.get("customer"); sub_end = None
            if sub_id:
                sub = await s_get(f"subscriptions/{sub_id}")
                sub_end = sub.get("current_period_end")
            await db_grant_premium(s["email"], cid or "", sub_id or "", sub_end)
            log.info(f"Premium granted (success page): {s['email']}")
    except Exception as e:
        log.warning(f"Success page verify: {e}")
    return RedirectResponse("/?welcome=1", 302)

# ── Stripe: customer portal (manage/cancel) ───────────────────────────
@app.post("/stripe/portal")
async def portal(req: Request):
    s = auth_session(req)
    if not s: raise HTTPException(401)
    row = await db_get(s["email"])
    if not row or not row.get("stripe_customer_id"): raise HTTPException(400, "No Stripe customer")
    p = await s_post("billing_portal/sessions",
                     {"customer": row["stripe_customer_id"], "return_url": BASE_URL})
    return JSONResponse({"url": p["url"]})

# ── Stripe: webhook ───────────────────────────────────────────────────
@app.post("/stripe/webhook")
async def webhook(req: Request):
    body = await req.body()
    sig  = req.headers.get("stripe-signature","")
    if not stripe_sig_ok(body, sig):
        log.warning("Bad webhook signature"); raise HTTPException(400, "Bad signature")
    evt  = json.loads(body)
    etype = evt.get("type",""); obj = evt.get("data",{}).get("object",{})
    log.info(f"Webhook: {etype}")

    if etype == "checkout.session.completed":
        cid = obj.get("customer"); sub_id = obj.get("subscription")
        u = await db_get_by_customer(cid)
        if u:
            sub_end = None
            if sub_id:
                try: sub_end = (await s_get(f"subscriptions/{sub_id}")).get("current_period_end")
                except: pass
            await db_grant_premium(u["email"], cid, sub_id or "", sub_end)
            log.info(f"Webhook: premium → {u['email']}")

    elif etype == "customer.subscription.deleted":
        u = await db_get_by_customer(obj.get("customer"))
        if u: await db_revoke_premium(u["email"]); log.info(f"Webhook: revoked → {u['email']}")

    elif etype == "invoice.payment_succeeded":
        cid = obj.get("customer"); sub_id = obj.get("subscription")
        u = await db_get_by_customer(cid)
        if u and sub_id:
            try:
                sub = await s_get(f"subscriptions/{sub_id}")
                await db_grant_premium(u["email"], cid, sub_id, sub.get("current_period_end"))
                log.info(f"Webhook: renewed → {u['email']}")
            except Exception as e: log.warning(f"Renewal: {e}")

    elif etype == "invoice.payment_failed":
        u = await db_get_by_customer(obj.get("customer"))
        if u: log.warning(f"Payment failed for {u.get('email')} — Stripe will retry")

    return JSONResponse({"ok": True})

# ── Yahoo Finance proxy ───────────────────────────────────────────────
_yfc: dict = {}

@app.get("/api/yf")
async def yf_proxy(req: Request, sym: str = Query(...),
                   interval: str = Query("5m"), range: str = Query("30d")):
    require_api_auth(req)
    k = f"{sym}|{interval}|{range}"; ttl = 300 if interval in ("1d","1wk") else 30
    if (c := _yfc.get(k)) and time.time()-c[0] < ttl: return JSONResponse(c[1])
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(sym)}"
           f"?interval={interval}&range={range}&includePrePost=true&events=div%2Csplit")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers=YF_HEADERS, follow_redirects=True)
        if r.status_code != 200:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(url.replace("query1","query2"), headers=YF_HEADERS, follow_redirects=True)
        r.raise_for_status(); data = r.json()
        _yfc[k] = (time.time(), data); return JSONResponse(data)
    except Exception as e: raise HTTPException(502, str(e))

@app.get("/api/internals")
async def internals(req: Request):
    require_api_auth(req)
    if (c := _yfc.get("int")) and time.time()-c[0] < 60: return JSONResponse(c[1])
    SYMS = {"QQQ":"QQQ","IWM":"IWM","VIX":"%5EVIX","GLD":"GLD","TLT":"TLT","NVDA":"NVDA","TSLA":"TSLA"}
    out = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for lbl, ys in SYMS.items():
            try:
                r  = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ys}"
                                      f"?interval=1d&range=2d", headers=YF_HEADERS)
                cl = [x for x in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close",[]) if x]
                if cl: out[lbl] = {"price": round(cl[-1],2),
                                   "chgPct": round((cl[-1]-cl[-2])/cl[-2]*100,2) if len(cl)>1 else 0}
            except: pass
    _yfc["int"] = (time.time(), out); return JSONResponse(out)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.environ.get("PORT",8000)))
