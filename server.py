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
    # Ensure the parent directory exists (important for /data volume mounts)
    import pathlib
    pathlib.Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
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
@app.get("/debug/login")
async def debug_login(req: Request):
    """Shows exact redirect_uri being sent to Google — check this matches Google Console."""
    redirect = f"{BASE_URL}/auth/callback"
    return JSONResponse({
        "BASE_URL": BASE_URL,
        "redirect_uri": redirect,
        "add_this_to_google_console": redirect,
    })

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
@app.get("/stripe/portal")   # GET so browser can navigate directly
@app.post("/stripe/portal")  # POST kept for JS fetch calls
async def portal(req: Request):
    s = auth_session(req)
    if not s: return RedirectResponse("/login", 302)
    row = await db_get(s["email"])
    if not row or not row.get("stripe_customer_id"):
        # No Stripe customer yet — send to upgrade page
        return RedirectResponse("/?no_sub=1", 302)
    try:
        p = await s_post("billing_portal/sessions",
                         {"customer": row["stripe_customer_id"], "return_url": BASE_URL})
        url = p["url"]
        # For GET requests (direct navigation), redirect immediately
        if req.method == "GET":
            return RedirectResponse(url, 302)
        return JSONResponse({"url": url})
    except Exception as e:
        log.error(f"Portal error for {s['email']}: {e}")
        raise HTTPException(500, f"Billing portal error: {str(e)[:200]}")

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

# ── Admin endpoints ──────────────────────────────────────────────────
@app.get("/admin/users")
async def admin_users(req: Request, secret: str = Query(...)):
    """View all users. Visit: /admin/users?secret=FIRST16CHARS_OF_SECRET_KEY"""
    if secret != SECRET_KEY[:16]:
        raise HTTPException(403, "Forbidden")
    import datetime
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT email, name, is_premium, stripe_customer_id, "
            "subscription_id, subscription_end, created_at FROM users ORDER BY created_at DESC"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        if r.get("subscription_end"):
            r["sub_end_date"] = datetime.datetime.fromtimestamp(
                r["subscription_end"], tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
    return JSONResponse({"count": len(rows), "users": rows})

@app.post("/admin/grant")
async def admin_grant(req: Request, secret: str = Query(...), email: str = Query(...)):
    """Manually grant 30-day premium. POST /admin/grant?secret=xxx&email=user@gmail.com"""
    if secret != SECRET_KEY[:16]:
        raise HTTPException(403, "Forbidden")
    sub_end = int(time.time()) + 86400 * 30
    await db_grant_premium(email, "manual", "manual", sub_end)
    return JSONResponse({"granted": True, "email": email})

@app.post("/admin/reset_customer")
async def admin_reset_customer(req: Request, secret: str = Query(...), email: str = Query(...)):
    """Clear stripe_customer_id so a fresh one gets created on next checkout."""
    if secret != SECRET_KEY[:16]:
        raise HTTPException(403, "Forbidden")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET stripe_customer_id=NULL, subscription_id=NULL WHERE email=?",
            (email,))
        await db.commit()
    return JSONResponse({"reset": True, "email": email})

@app.post("/admin/set_customer")
async def admin_set_customer(req: Request, secret: str = Query(...),
                              email: str = Query(...), customer_id: str = Query(...)):
    """Set a specific Stripe customer ID for a user.
    POST /admin/set_customer?secret=xxx&email=user@gmail.com&customer_id=cus_xxx"""
    if secret != SECRET_KEY[:16]:
        raise HTTPException(403, "Forbidden")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET stripe_customer_id=? WHERE email=?",
            (customer_id, email))
        await db.commit()
    row = await db_get(email)
    return JSONResponse({"updated": True, "email": email,
                         "stripe_customer_id": row.get("stripe_customer_id")})

# ── Yahoo Finance proxy ───────────────────────────────────────────────
_yfc: dict = {}
_yf_cookies: dict = {}      # shared cookie jar across requests
_yf_crumb:   str  = ""      # crumb token (required by YF since 2023)
_yf_crumb_ts: float = 0

YF_URLS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]

YF_HEADERS_FULL = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

YF_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
}


async def yf_get_crumb(client: httpx.AsyncClient) -> str:
    """Fetch a valid YF crumb + set session cookies. Required since 2023."""
    global _yf_crumb, _yf_crumb_ts, _yf_cookies
    if _yf_crumb and (time.time() - _yf_crumb_ts) < 3600:
        return _yf_crumb
    try:
        # Step 1: visit finance.yahoo.com to get consent cookies
        r1 = await client.get("https://finance.yahoo.com",
                              headers=YF_HEADERS_FULL, follow_redirects=True, timeout=10)
        # Step 2: fetch crumb
        r2 = await client.get("https://query2.finance.yahoo.com/v1/test/getcrumb",
                              headers=YF_API_HEADERS, timeout=10)
        if r2.status_code == 200 and r2.text.strip():
            _yf_crumb    = r2.text.strip()
            _yf_crumb_ts = time.time()
            log.info(f"YF crumb acquired: {_yf_crumb[:8]}…")
            return _yf_crumb
    except Exception as e:
        log.warning(f"Crumb fetch failed: {e}")
    return ""


async def yf_fetch_chart(sym: str, interval: str, range_: str) -> dict:
    """Fetch YF chart data with crumb auth and automatic retry."""
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                  limits=limits) as client:
        crumb = await yf_get_crumb(client)

        for base_url in YF_URLS:
            params = {
                "interval":        interval,
                "range":           range_,
                "includePrePost":  "true",
                "events":          "div,split",
            }
            if crumb:
                params["crumb"] = crumb

            url = f"{base_url}/v8/finance/chart/{quote(sym)}"
            try:
                r = await client.get(url, params=params, headers=YF_API_HEADERS)
                log.info(f"YF {sym} {interval} {range_} → {r.status_code} ({base_url.split('.')[1]})")

                if r.status_code == 401:
                    # Crumb expired — refresh and retry once
                    _yf_crumb_ts = 0
                    crumb = await yf_get_crumb(client)
                    if crumb:
                        params["crumb"] = crumb
                        r = await client.get(url, params=params, headers=YF_API_HEADERS)

                if r.status_code == 200:
                    return r.json()

                log.warning(f"YF {base_url} → {r.status_code}: {r.text[:200]}")

            except Exception as e:
                log.warning(f"YF {base_url} error: {e}")
                continue

    raise HTTPException(502, f"Yahoo Finance unavailable for {sym} — all endpoints failed")


@app.get("/api/yf")
async def yf_proxy(req: Request, sym: str = Query(...),
                   interval: str = Query("5m"), range: str = Query("30d")):
    require_api_auth(req)
    k = f"{sym}|{interval}|{range}"; ttl = 300 if interval in ("1d","1wk") else 30
    if (c := _yfc.get(k)) and time.time()-c[0] < ttl:
        return JSONResponse(c[1])
    data = await yf_fetch_chart(sym, interval, range)
    _yfc[k] = (time.time(), data)
    return JSONResponse(data)


@app.get("/api/internals")
async def internals(req: Request):
    require_api_auth(req)
    if (c := _yfc.get("int")) and time.time()-c[0] < 60:
        return JSONResponse(c[1])
    SYMS = {"QQQ":"QQQ","IWM":"IWM","VIX":"%5EVIX","GLD":"GLD","TLT":"TLT","NVDA":"NVDA","TSLA":"TSLA"}
    out = {}
    for lbl, ys in SYMS.items():
        try:
            data = await yf_fetch_chart(ys, "1d", "5d")
            q  = data["chart"]["result"][0]["indicators"]["quote"][0]
            cl = [x for x in (q.get("close") or []) if x]
            if cl:
                out[lbl] = {"price":  round(cl[-1], 2),
                            "chgPct": round((cl[-1]-cl[-2])/cl[-2]*100, 2) if len(cl)>1 else 0}
        except Exception as e:
            log.warning(f"Internals {lbl}: {e}")
    _yfc["int"] = (time.time(), out)
    return JSONResponse(out)


@app.get("/api/debug")
async def debug_yf(req: Request, sym: str = Query("SPY")):
    """Debug endpoint — tests YF connectivity. Remove in production."""
    require_api_auth(req)
    results = {}
    try:
        data = await yf_fetch_chart(sym, "1d", "5d")
        ts = data.get("chart",{}).get("result",[{}])[0].get("timestamp",[])
        results["status"] = "ok"
        results["bars"]   = len(ts)
        results["crumb"]  = _yf_crumb[:8] + "…" if _yf_crumb else "none"
    except Exception as e:
        results["status"] = "error"
        results["error"]  = str(e)
    return JSONResponse(results)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=int(os.environ.get("PORT",8000)))
