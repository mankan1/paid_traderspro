#!/usr/bin/env python3
"""
build.py — Patch oracle_yf_dashboard.html for server deployment
================================================================
Run:  python build.py

Patches applied:
  1. yfFetch()         → /api/yf         (server-side YF proxy, no CORS)
  2. yfFetchInternals()→ /api/internals  (batched, server-cached)
  3. Nav bar          ← Google avatar + logout + premium badge
  4. /me fetch        ← on page load (auth + premium status)
  5. PAYWALL ENGINE   ← 60s free preview, then blur + upgrade modal
"""

import sys
from pathlib import Path

SRC  = Path(__file__).parent / "oracle_yf_dashboard.html"
DEST = Path(__file__).parent / "templates" / "dashboard.html"

if not SRC.exists():
    print(f"ERROR: {SRC} not found.")
    sys.exit(1)

DEST.parent.mkdir(parents=True, exist_ok=True)
html = SRC.read_text("utf-8")

# ─────────────────────────────────────────────────────────────────
# PATCH 1 — yfFetch → /api/yf
# ─────────────────────────────────────────────────────────────────
OLD = """\
// ── Fetch YF chart JSON (via allorigins CORS proxy) ──────────────
async function yfFetch(yfSym, interval, rangeDays) {
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(yfSym)}` +
    `?interval=${interval}&range=${rangeDays}d&includePrePost=true&events=div%2Csplit`;
  const cors = `https://api.allorigins.win/get?url=${encodeURIComponent(url)}`;
  const resp = await fetch(cors, { cache: 'no-store' });
  if (!resp.ok) throw new Error(`allorigins ${resp.status}`);
  const wrapper = await resp.json();
  return JSON.parse(wrapper.contents);
}"""

NEW = """\
// ── Fetch YF chart JSON via server proxy ─────────────────────────
async function yfFetch(yfSym, interval, rangeDays) {
  const url = `/api/yf?sym=${encodeURIComponent(yfSym)}&interval=${interval}&range=${rangeDays}d`;
  const r = await fetch(url, { cache: 'no-store', credentials: 'same-origin' });
  if (r.status === 401) { window.location.href = '/login'; throw new Error('auth'); }
  if (!r.ok) throw new Error(`proxy ${r.status}`);
  return r.json();
}"""

assert OLD in html, "PATCH 1 FAILED: yfFetch not found"
html = html.replace(OLD, NEW)
print("✓ Patch 1: yfFetch → /api/yf")

# ─────────────────────────────────────────────────────────────────
# PATCH 2 — yfFetchInternals → /api/internals
# ─────────────────────────────────────────────────────────────────
OLD2 = """\
// ── Fetch internals (QQQ IWM VIX GLD TLT NVDA TSLA) ─────────────
async function yfFetchInternals() {
  const syms = ['QQQ','IWM','%5EVIX','GLD','TLT','NVDA','TSLA'];
  const symLabels = ['QQQ','IWM','VIX','GLD','TLT','NVDA','TSLA'];
  const result = {};
  await Promise.allSettled(syms.map(async (yfSym, i) => {
    try {
      const data = await yfFetch(yfSym, '1d', 2);
      const res  = data?.chart?.result?.[0];
      if (!res) return;
      const q    = res.indicators?.quote?.[0] || {};
      const closes = q.close || [];
      const price  = closes[closes.length - 1];
      const prev   = closes[closes.length - 2] || price;
      if (!price) return;
      result[symLabels[i]] = {
        price:  +price.toFixed(2),
        chgPct: prev ? +((price - prev) / prev * 100).toFixed(2) : 0,
      };
    } catch(e) { /* skip failed internals */ }
  }));
  return result;
}"""

NEW2 = """\
// ── Fetch internals via server (batched + cached) ─────────────────
async function yfFetchInternals() {
  try {
    const r = await fetch('/api/internals', { credentials: 'same-origin' });
    if (!r.ok) return {};
    return r.json();
  } catch(e) { return {}; }
}"""

assert OLD2 in html, "PATCH 2 FAILED: yfFetchInternals not found"
html = html.replace(OLD2, NEW2)
print("✓ Patch 2: yfFetchInternals → /api/internals")

# ─────────────────────────────────────────────────────────────────
# PATCH 3 — Nav bar: Google badge + premium indicator
# ─────────────────────────────────────────────────────────────────
NAV_INJECT = """\
  <!-- Google user badge — flex-shrink:0 keeps it always visible -->
  <div id="oracleUserBadge" style="display:none;align-items:center;gap:8px;
       padding:0 10px;flex-shrink:0;border-left:1px solid rgba(255,255,255,.07);margin-left:8px">
    <span id="premiumBadgeNav" style="display:none;font-family:var(--font-mono);font-size:8px;
          letter-spacing:1.5px;color:#f0c040;border:1px solid rgba(240,192,64,.4);
          padding:2px 7px;border-radius:2px;background:rgba(240,192,64,.08);white-space:nowrap">⚡ PRO</span>
    <img id="oracleAvatar" src="" width="24" height="24"
         style="border-radius:50%;border:1px solid rgba(255,255,255,.15);flex-shrink:0" />
    <span id="oracleUserName" style="font-family:var(--font-mono);font-size:10px;color:var(--t2);
          white-space:nowrap;max-width:110px;overflow:hidden;text-overflow:ellipsis"></span>
    <a href="/stripe/portal" id="manageSubBtn" style="display:none;white-space:nowrap;
            font-family:var(--font-mono);font-size:9px;color:var(--t3);cursor:pointer;
            border:1px solid var(--border2);padding:2px 8px;border-radius:2px;
            background:rgba(255,255,255,.04);text-decoration:none">MANAGE SUB</a>
    <a href="/logout" style="font-family:var(--font-mono);font-size:9px;color:var(--t3);
       text-decoration:none;border:1px solid var(--border2);padding:2px 8px;border-radius:2px;
       background:rgba(255,255,255,.04);white-space:nowrap">LOGOUT</a>
  </div>
</nav>"""

assert "</nav>" in html, "PATCH 3 FAILED: </nav> not found"
html = html.replace("</nav>", NAV_INJECT, 1)
print("✓ Patch 3: Nav user badge injected")

# ─────────────────────────────────────────────────────────────────
# PATCH 4 — Paywall CSS (injected into <head>)
# ─────────────────────────────────────────────────────────────────
PAYWALL_CSS = """\
<style id="paywallCss">
/* ── Paywall overlay ─────────────────────────────────────────── */
#paywallOverlay {
  display: none;
  position: fixed; inset: 0; z-index: 9000;
  background: rgba(3,4,7,.72);
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
  align-items: center; justify-content: center;
}
#paywallOverlay.active { display: flex; }

#paywallModal {
  background: linear-gradient(160deg,#0d1420 0%,#0a0f1a 100%);
  border: 1px solid rgba(240,192,64,.35);
  border-radius: 6px;
  padding: 40px 44px 36px;
  max-width: 480px; width: 92%;
  box-shadow: 0 0 80px rgba(0,0,0,.8), 0 0 40px rgba(240,192,64,.08);
  text-align: center;
  position: relative;
  animation: pmIn .32s cubic-bezier(.34,1.56,.64,1);
}
@keyframes pmIn { from { transform: scale(.88); opacity:0 } to { transform:scale(1); opacity:1 } }

.pm-lock { font-size: 38px; margin-bottom: 10px; }
.pm-title {
  font-family: var(--font-mono); font-size: 22px; font-weight: 700;
  color: #f0c040; letter-spacing: 2px; margin-bottom: 6px;
}
.pm-sub {
  font-family: var(--font-mono); font-size: 11px; color: var(--t3);
  letter-spacing: 1px; margin-bottom: 28px;
}
.pm-features {
  list-style: none; padding: 0; margin: 0 0 28px;
  display: grid; grid-template-columns: 1fr 1fr; gap: 10px 16px;
  text-align: left;
}
.pm-features li {
  font-family: var(--font-mono); font-size: 10px; color: var(--t2);
  display: flex; align-items: center; gap: 7px;
}
.pm-features li::before { content: "✓"; color: #4dff91; font-size: 11px; flex-shrink: 0; }
.pm-price {
  font-family: var(--font-mono); font-size: 32px; font-weight: 700;
  color: var(--t1); margin-bottom: 4px;
}
.pm-period { font-family: var(--font-mono); font-size: 10px; color: var(--t3); margin-bottom: 24px; }
.pm-btn {
  display: block; width: 100%;
  background: linear-gradient(90deg,#e8b020,#f0c040,#e8b020);
  background-size: 200%;
  border: none; border-radius: 4px;
  font-family: var(--font-mono); font-size: 13px; font-weight: 700;
  color: #0a0c10; letter-spacing: 2px;
  padding: 14px 0; cursor: pointer;
  transition: background-position .4s, transform .1s, box-shadow .2s;
  box-shadow: 0 0 24px rgba(240,192,64,.3);
}
.pm-btn:hover {
  background-position: right;
  box-shadow: 0 0 40px rgba(240,192,64,.5);
  transform: translateY(-1px);
}
.pm-btn:active { transform: translateY(0); }
.pm-btn:disabled { opacity:.6; cursor:wait; }
.pm-guarantee {
  font-family: var(--font-mono); font-size: 9px; color: var(--t3);
  margin-top: 14px; letter-spacing: .5px;
}
.pm-login-link {
  font-family: var(--font-mono); font-size: 10px; color: var(--t3);
  margin-top: 10px;
}
.pm-login-link a { color: var(--ice); text-decoration: none; }
.pm-dismiss { display: none; }  /* hidden unless 1-time preview granted */

/* ── Preview countdown banner ──────────────────────────────────── */
#previewBanner {
  display: none;
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 8000;
  background: linear-gradient(90deg,rgba(240,192,64,.12),rgba(240,192,64,.06));
  border-top: 1px solid rgba(240,192,64,.3);
  padding: 8px 20px;
  display: flex; align-items: center; gap: 12px;
}
#previewBanner.active { display: flex; }
.pb-text {
  font-family: var(--font-mono); font-size: 10px; color: var(--solar);
  letter-spacing: 1px; flex: 1;
}
#previewTimer {
  font-family: var(--font-mono); font-size: 14px; font-weight: 700;
  color: #f0c040; min-width: 28px; text-align: right;
}
.pb-cta {
  font-family: var(--font-mono); font-size: 10px; font-weight: 700;
  color: #0a0c10; background: #f0c040; border: none;
  padding: 5px 14px; border-radius: 3px; cursor: pointer; letter-spacing: 1px;
}

/* ── Premium lock: blur + no-pointer on locked panels ──────────── */
.premium-locked {
  position: relative; pointer-events: none;
  user-select: none; -webkit-user-select: none;
}
.premium-locked::after {
  content: "";
  position: absolute; inset: 0; z-index: 10;
  backdrop-filter: blur(7px);
  -webkit-backdrop-filter: blur(7px);
  background: rgba(3,4,7,.35);
  border-radius: inherit;
}
</style>
</head>"""

assert "</head>" in html, "PATCH 4 FAILED: </head> not found"
html = html.replace("</head>", PAYWALL_CSS, 1)
print("✓ Patch 4: Paywall CSS injected")

# ─────────────────────────────────────────────────────────────────
# PATCH 5 — Paywall HTML (modal + countdown banner, injected into <body>)
# ─────────────────────────────────────────────────────────────────
PAYWALL_HTML = """\
<body>

<!-- ══ PAYWALL OVERLAY ══════════════════════════════════════════ -->
<div id="paywallOverlay">
  <div id="paywallModal">
    <div class="pm-lock">🔒</div>
    <div class="pm-title">0DTE ORACLE PRO</div>
    <div class="pm-sub" id="pmSub">YOUR FREE PREVIEW HAS ENDED</div>
    <ul class="pm-features">
      <li>Pattern Recognition</li>
      <li>ML Signal Engine</li>
      <li>Multi-TF Confluence</li>
      <li>Options Flow Panel</li>
      <li>Trade Ideas &amp; Alerts</li>
      <li>Backtest Engine</li>
      <li>Market Internals</li>
      <li>C++ HFT Engine</li>
    </ul>
    <div class="pm-price">$15<span style="font-size:16px;color:var(--t3)">/mo</span></div>
    <div class="pm-period">CANCEL ANYTIME · SECURE STRIPE CHECKOUT</div>

    <!-- Shown to logged-in users -->
    <button class="pm-btn" id="upgradeBtn" onclick="startCheckout()" style="display:none">
      ⚡ UPGRADE TO PRO — $15/mo
    </button>

    <!-- Shown to guests (not logged in) -->
    <a href="/login" id="googleSignInBtn" class="pm-btn"
       style="display:flex;align-items:center;justify-content:center;gap:10px;text-decoration:none">
      <svg width="18" height="18" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
        <path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.717v2.258h2.908c1.702-1.567 2.684-3.874 2.684-6.615z"/>
        <path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 0 0 9 18z"/>
        <path fill="#FBBC05" d="M3.964 10.71A5.41 5.41 0 0 1 3.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 0 0 0 9c0 1.452.348 2.827.957 4.042l3.007-2.332z"/>
        <path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 0 0 .957 4.958L3.964 6.29C4.672 4.163 6.656 3.58 9 3.58z"/>
      </svg>
      SIGN IN WITH GOOGLE TO UPGRADE
    </a>

    <div class="pm-guarantee">30-day money-back guarantee · No lock-in</div>
    <div style="margin-top:12px;font-family:var(--font-mono);font-size:9px;color:var(--t3);letter-spacing:.5px">
      Questions? <a href="mailto:marketgurus8@gmail.com" style="color:var(--ice);text-decoration:none">marketgurus8@gmail.com</a>
    </div>
  </div>
</div>

<!-- ══ PREVIEW COUNTDOWN BANNER ══════════════════════════════════ -->
<div id="previewBanner" style="display:none">
  <span class="pb-text">⚡ FREE PREVIEW — Full dashboard unlocks for <span id="previewTimer">60</span>s</span>
  <button class="pb-cta" onclick="showPaywall()">UPGRADE TO PRO →</button>
</div>"""

import re as _re
_bm = _re.search(r"<body[^>]*>", html)
assert _bm, "PATCH 5 FAILED: <body> not found"
_inner = PAYWALL_HTML.split("<body>", 1)[-1]
html = html.replace(_bm.group(0), _bm.group(0) + "\n" + _inner, 1)
print("✓ Patch 5: Paywall modal + countdown banner injected")

# ─────────────────────────────────────────────────────────────────
# PATCH 6 — Paywall JS engine (injected just before </script>)
# Replaces the /me fetch block from the previous build step.
# ─────────────────────────────────────────────────────────────────

# The old /me fetch block (from previous build.py) — replace it entirely
OLD_ME = """\
  // Fetch current user from server and show in nav bar
  fetch('/me', { credentials: 'same-origin' })
    .then(r => r.json())
    .then(u => {
      if (!u.authenticated) { window.location.href = '/login'; return; }
      const badge = document.getElementById('googleUserBadge');
      const avatar = document.getElementById('googleAvatar');
      const name   = document.getElementById('googleUserName');
      if (badge)  badge.style.display = 'flex';
      if (avatar && u.picture) avatar.src = u.picture;
      if (name)   name.textContent = u.name || u.email || '';
    })
    .catch(() => {});
});"""

# Check if old build.py has already run (it will have injected the old ME block)
# If not found, inject our new block after the wlStartYfFuturesPoll line instead
OLD_POLL = """\
  // Kick off YF futures price polling for the watchlist sidebar (no API key needed)
  setTimeout(wlStartYfFuturesPoll, 2000);
});"""

PAYWALL_JS = """\
// ═══════════════════════════════════════════════════════════════
// PAYWALL ENGINE
// ═══════════════════════════════════════════════════════════════
(function() {
  'use strict';

  // ── IDs of premium-locked panels (blurred after preview) ─────
  const PREMIUM_PANELS = [
    // Right column panels
    'mlPanel', 'flowPanel', 'alertFeed',
    // Bottom row
    'patternPanel', 'tradeIdeas',
    // Grid panels by class — handled via querySelectorAll below
  ];
  // Extra CSS selectors for locked areas
  const PREMIUM_SELECTORS = [
    '.g-internals', '.g-tod', '.g-forecast', '.g-trades',
    '.g-right-col',        // entire right column
    '#btResultsPanel',     // backtest results
    '.ob-panel',           // options builder
    '#mtfGrid',            // multi-TF grid inside throne
    '#probCanvas',         // probability gauge
    '#recBox',             // trade recommendation box
  ];

  let _previewSeconds = 60;
  let _isPremium      = false;
  let _previewTimer   = null;
  let _countdownInterval = null;
  let _previewActive  = false;
  let _previewEnded   = false;

  // ── Init: fetch /me, decide free vs premium ────────────────
  async function initPaywall() {
    // Hide banner immediately — show only if user is confirmed free tier
    const _banner = document.getElementById('previewBanner');
    if (_banner) _banner.style.display = 'none';

    let me;
    try {
      const r = await fetch('/me', { credentials: 'same-origin' });
      if (!r.ok) { window.location.href = '/login'; return; }
      me = await r.json();
    } catch(e) { return; }

    if (!me.authenticated) { window.location.href = '/login'; return; }

    // Populate nav badge
    const badge  = document.getElementById('oracleUserBadge');
    const avatar = document.getElementById('oracleAvatar');
    const uname  = document.getElementById('oracleUserName');
    const premBadge = document.getElementById('premiumBadgeNav');
    const manageBtn = document.getElementById('manageSubBtn');
    if (badge)  badge.style.display  = 'flex';
    if (avatar && me.picture) avatar.src = me.picture;
    if (uname)  uname.textContent    = me.name || me.email || '';

    _isPremium      = !!me.premium;
    _previewSeconds = me.preview_seconds || 60;

    // Store Stripe pub key for checkout
    if (me.stripe_pub_key) window._stripeKey = me.stripe_pub_key;

    if (_isPremium) {
      // ── PREMIUM USER — show badge, no paywall ─────────────
      if (premBadge) premBadge.style.display = 'inline';
      if (manageBtn) manageBtn.style.display  = 'inline-block';
      const _pb = document.getElementById('previewBanner');
      if (_pb) { _pb.style.display = 'none'; _pb.classList.remove('active'); }
      unlockAll();
      // Poll to detect cancellation (every 5 min)
      setInterval(recheckPremium, 5 * 60 * 1000);

      // Welcome back message
      const urlP = new URLSearchParams(location.search);
      if (urlP.get('welcome') === '1') showWelcomeToast();

    } else if (me.authenticated) {
      // ── LOGGED IN FREE USER — show upgrade button ─────────
      const upgradeBtn   = document.getElementById('upgradeBtn');
      const googleBtn    = document.getElementById('googleSignInBtn');
      if (upgradeBtn) upgradeBtn.style.display = 'block';
      if (googleBtn)  googleBtn.style.display  = 'none';
      startPreview();

    } else {
      // ── GUEST (not logged in) — show Google sign-in ───────
      const upgradeBtn   = document.getElementById('upgradeBtn');
      const googleBtn    = document.getElementById('googleSignInBtn');
      const sub          = document.getElementById('pmSub');
      if (upgradeBtn) upgradeBtn.style.display = 'none';
      if (googleBtn)  googleBtn.style.display  = 'flex';
      if (sub) sub.textContent = 'SIGN IN TO UNLOCK ALL FEATURES';
      // Hide nav badge for guests
      if (badge) badge.style.display = 'none';
      startPreview();
    }
  }

  function startPreview() {
    _previewActive = true;
    const banner  = document.getElementById('previewBanner');
    const timerEl = document.getElementById('previewTimer');
    if (banner)  { banner.style.display = 'flex'; banner.classList.add('active'); }
    if (timerEl) timerEl.textContent = _previewSeconds;

    let remaining = _previewSeconds;
    _countdownInterval = setInterval(() => {
      remaining -= 1;
      if (timerEl) timerEl.textContent = Math.max(0, remaining);
      if (remaining <= 10 && timerEl) timerEl.style.color = 'var(--plasma)';
      if (remaining <= 0) {
        clearInterval(_countdownInterval);
        endPreview();
      }
    }, 1000);
  }

  function endPreview() {
    _previewActive = false;
    _previewEnded  = true;
    const banner = document.getElementById('previewBanner');
    if (banner) { banner.style.display = 'none'; banner.classList.remove('active'); }
    lockPremiumPanels();
    showPaywall();
  }

  // ── Lock: blur premium panels ─────────────────────────────
  function lockPremiumPanels() {
    PREMIUM_PANELS.forEach(id => {
      const el = document.getElementById(id);
      if (el && el.closest) el.classList.add('premium-locked');
    });
    PREMIUM_SELECTORS.forEach(sel => {
      document.querySelectorAll(sel).forEach(el => el.classList.add('premium-locked'));
    });
    // Also disable backtest + stream buttons
    ['btEnterBtn','streamBtn'].forEach(id => {
      const b = document.getElementById(id);
      if (b) { b.disabled = true; b.title = 'Upgrade to Pro'; }
    });
  }

  function unlockAll() {
    document.querySelectorAll('.premium-locked').forEach(el => el.classList.remove('premium-locked'));
    ['btEnterBtn','streamBtn'].forEach(id => {
      const b = document.getElementById(id);
      if (b) { b.disabled = false; b.title = ''; }
    });
  }

  // ── Paywall modal ─────────────────────────────────────────
  window.showPaywall = function() {
    document.getElementById('paywallOverlay').classList.add('active');
  };

  window.hidePaywall = function() {
    document.getElementById('paywallOverlay').classList.remove('active');
  };

  // ── Stripe checkout ───────────────────────────────────────
  window.startCheckout = async function() {
    // If not logged in, send to Google login first
    const meCheck = await fetch('/me', { credentials: 'same-origin' }).then(r => r.json()).catch(() => ({}));
    if (!meCheck.authenticated) { window.location.href = '/login'; return; }

    const btn = document.getElementById('upgradeBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'REDIRECTING…'; }
    try {
      const r = await fetch('/stripe/checkout', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      });
      if (r.status === 401) { window.location.href = '/login'; return; }
      if (!r.ok) throw new Error(await r.text());
      const { url } = await r.json();
      window.location.href = url;
    } catch(e) {
      if (btn) { btn.disabled = false; btn.textContent = '⚡ UPGRADE TO PRO — $15/mo'; }
      alert('Checkout error: ' + e.message);
    }
  };

  // ── Manage subscription (Stripe portal) ───────────────────
  window.openManageSub = function() {
    window.location.href = '/stripe/portal';
  };

  // ── Re-check premium status (after returning from Stripe) ─
  async function recheckPremium() {
    try {
      const r  = await fetch('/me', { credentials: 'same-origin' });
      const me = await r.json();
      if (me.premium && !_isPremium) {
        _isPremium = true;
        unlockAll();
        hidePaywall();
        const _pb2 = document.getElementById('previewBanner');
        if (_pb2) { _pb2.style.display = 'none'; _pb2.classList.remove('active'); }
        if (_countdownInterval) { clearInterval(_countdownInterval); _countdownInterval = null; }
        const b = document.getElementById('premiumBadgeNav');
        if (b) b.style.display = 'inline';
        showWelcomeToast();
      } else if (!me.premium && _isPremium) {
        _isPremium = false;
        window.location.reload();
      }
    } catch(e) {}
  }

  // Poll for premium upgrade while paywall is open (handles Stripe redirect back)
  setInterval(() => {
    if (!_isPremium) recheckPremium();
  }, 8000);

  // ── Welcome toast ─────────────────────────────────────────
  function showWelcomeToast() {
    const t = document.createElement('div');
    t.style.cssText = `
      position:fixed;bottom:24px;right:24px;z-index:9999;
      background:linear-gradient(90deg,#1a2010,#0d1a08);
      border:1px solid rgba(77,255,145,.4);border-radius:4px;
      padding:14px 20px;font-family:var(--font-mono);
      font-size:11px;color:#4dff91;letter-spacing:1px;
      box-shadow:0 0 32px rgba(77,255,145,.2);
      animation:pmIn .3s ease;
    `;
    t.innerHTML = '⚡ Welcome to 0DTE Oracle Pro! All features unlocked.';
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 5000);
  }

  // ── Overlay click outside modal to dismiss (free users can close once) ─
  document.getElementById('paywallOverlay').addEventListener('click', function(e) {
    if (e.target === this && _previewActive && !_previewEnded) hidePaywall();
  });

  // ── Boot ──────────────────────────────────────────────────
  initPaywall();

  // ── Re-check when page becomes visible (tab switch back) ─
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !_isPremium) recheckPremium();
  });

})();
"""

# Try to replace the old /me block if it exists (from a previous build run)
if OLD_ME in html:
    html = html.replace(OLD_ME, PAYWALL_JS + "\n});", 1)
    print("✓ Patch 6: Replaced old /me block with Paywall JS engine")
elif OLD_POLL in html:
    NEW_POLL = """\
  // Kick off YF futures price polling for the watchlist sidebar (no API key needed)
  setTimeout(wlStartYfFuturesPoll, 2000);
});\n""" + PAYWALL_JS
    html = html.replace(OLD_POLL, NEW_POLL, 1)
    print("✓ Patch 6: Paywall JS engine injected after poll block")
else:
    # Last resort: inject before </script>
    html = html.replace("</script>", PAYWALL_JS + "\n</script>", 1)
    print("✓ Patch 6: Paywall JS engine injected before </script>")

# ─────────────────────────────────────────────────────────────────
# PATCH 7 — Hide hint bar permanently on hosted version
# The hint bar says "run yf_proxy.py locally" — irrelevant on Railway
# ─────────────────────────────────────────────────────────────────

# 7a. Hide the hint bar HTML element by default
OLD_HINTBAR = '<div class="hint-bar" id="hintBar" style="background:rgba(255,214,10,.06);border-color:rgba(255,214,10,.2)">'
NEW_HINTBAR = '<div class="hint-bar" id="hintBar" style="display:none!important;background:rgba(255,214,10,.06);border-color:rgba(255,214,10,.2)">'
if OLD_HINTBAR in html:
    html = html.replace(OLD_HINTBAR, NEW_HINTBAR)
    print("✓ Patch 7a: hint bar hidden in HTML")

# 7b. Remove the 8s timer that shows the hint bar
OLD_HINT_TIMER = """  // Show hint bar after 8s if no data loaded yet
  setTimeout(() => {
    if(!S.bars.length) {
      document.getElementById('hintBar').style.display = '';
    }
  }, 8000);"""
NEW_HINT_TIMER = """  // Hint bar suppressed on hosted version (no local proxy needed)"""
if OLD_HINT_TIMER in html:
    html = html.replace(OLD_HINT_TIMER, NEW_HINT_TIMER)
    print("✓ Patch 7b: hint bar timer removed")

# 7c. Also suppress the ibkrBar (YF / PROXY / STREAM status bar)
# On hosted version it shows confusing "Connecting to yf_proxy..." messages
# Replace with a clean "YAHOO FINANCE · LIVE" status indicator
OLD_IBKR_BAR = '<div class="ibkr-bar" id="ibkrBar">'
NEW_IBKR_BAR = '<div class="ibkr-bar" id="ibkrBar" style="display:none">'
if OLD_IBKR_BAR in html:
    html = html.replace(OLD_IBKR_BAR, NEW_IBKR_BAR)
    print("✓ Patch 7c: ibkr status bar hidden (not needed on hosted version)")

# ─────────────────────────────────────────────────────────────────
# PATCH 8 — Replace window load handler with direct-mode-first init
# On Railway there is no local WebSocket proxy — skip the 3s wait
# and go straight into direct Yahoo Finance REST mode immediately.
# ─────────────────────────────────────────────────────────────────
OLD_LOAD_INIT = """  // Try connecting to local yf_proxy.py (optional — enhances Alpaca watchlist)
  // This runs in background and never blocks the dashboard
  ibkrConnect();

  // If no proxy after 3s, switch to guaranteed direct mode
  setTimeout(() => {
    if(!WS.connected || WS._directMode) {
      _yfDirectMode();
    }
  }, 3000);"""

NEW_LOAD_INIT = """  // Hosted on Railway — no local proxy, go straight to direct YF mode
  _yfDirectMode();"""

if OLD_LOAD_INIT in html:
    html = html.replace(OLD_LOAD_INIT, NEW_LOAD_INIT)
    print("✓ Patch 8: window load → immediate direct YF mode (no proxy wait)")
else:
    print("✗ Patch 8: load init block not found — skipping")

# ─────────────────────────────────────────────────────────────────
# WRITE
# ─────────────────────────────────────────────────────────────────
DEST.write_text(html, "utf-8")
print(f"\n✅ Build complete → {DEST}")
print(f"   Lines: {len(html.splitlines()):,}   Size: {len(html):,} bytes")
