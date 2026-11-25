"""SahajSale demo shop: one codebase, four roles (storefront, cart, inventory, payments).

Request chain for a checkout:  storefront -> cart -> inventory   then   storefront -> payments

The services are deliberately small but real: they talk over HTTP, expose Prometheus
metrics, log what went wrong, and can be broken on purpose (see /admin/fault and the
INJECT_* env vars) so that NudgeBee has something genuine to investigate.

The storefront reads two Flagsmith flags:
  sale_mode                 -> lighter, faster checkout (no recommendations, cached stock,
                               deferred reservation) so customers can still pay
  urgency_banner_variant    -> "A" or "B" banner copy, for A/B testing
"""
import asyncio
import logging
import os
import random
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

ROLE = os.getenv("ROLE", "storefront")
logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {ROLE} %(levelname)s %(message)s")
log = logging.getLogger(ROLE)

CART_URL = os.getenv("CART_URL", "http://localhost:8002")
INVENTORY_URL = os.getenv("INVENTORY_URL", "http://localhost:8003")
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://localhost:8004")
SIDEKICK_URL = os.getenv("SIDEKICK_URL", "http://localhost:8000")
UP_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "2.0"))

FLAGSMITH_ENV_KEY = os.getenv("FLAGSMITH_ENV_KEY", "")
FLAGSMITH_API = os.getenv("FLAGSMITH_API_URL", "https://edge.api.flagsmith.com/api/v1").rstrip("/")

PRODUCTS = {
    "diya-set": {"name": "Hand-painted diya set", "name_kn": "ಕೈಬಣ್ಣದ ಹಣತೆ ಸೆಟ್", "name_hi": "हाथ से रंगा दीया सेट", "price": 399, "emoji": "🪔"},
    "rangoli-kit": {"name": "Rangoli colour kit", "name_kn": "ರಂಗೋಲಿ ಬಣ್ಣಗಳ ಕಿಟ್", "name_hi": "रंगोली रंग किट", "price": 249, "emoji": "🎨"},
    "sweet-box": {"name": "Kaju katli sweet box", "name_kn": "ಕಾಜು ಕಟ್ಲಿ ಸಿಹಿ ಪೆಟ್ಟಿಗೆ", "name_hi": "काजू कतली मिठाई का डिब्बा", "price": 799, "emoji": "🍬"},
    "silk-dupatta": {"name": "Banarasi silk dupatta", "name_kn": "ಬನಾರಸಿ ರೇಷ್ಮೆ ದುಪಟ್ಟಾ", "name_hi": "बनारसी रेशमी दुपट्टा", "price": 1499, "emoji": "🧣"},
    "fairy-lights": {"name": "Festive fairy lights", "name_kn": "ಹಬ್ಬದ ಅಲಂಕಾರ ದೀಪಗಳು", "name_hi": "त्योहार की सजावटी लाइटें", "price": 349, "emoji": "✨"},
    "gift-hamper": {"name": "Diwali gift hamper", "name_kn": "ದೀಪಾವಳಿ ಉಡುಗೊರೆ ಪೆಟ್ಟಿಗೆ", "name_hi": "दिवाली गिफ्ट हैम्पर", "price": 1999, "emoji": "🎁"},
}

# ---------------------------------------------------------------- fault injection
FAULT = {
    "latency_ms": int(os.getenv("INJECT_LATENCY_MS", "0")),
    "error_rate": float(os.getenv("INJECT_ERROR_RATE", "0")),
    "leak_mb": 0,
}
MAX_LEAK_MB = int(os.getenv("MAX_LEAK_MB", "150"))
_LEAK: list = []


def apply_leak(mb: int) -> None:
    target = min(max(mb, 0), MAX_LEAK_MB)
    while len(_LEAK) < target:
        _LEAK.append(b"x" * (1024 * 1024))  # real, touched memory
    while len(_LEAK) > target:
        _LEAK.pop()
    FAULT["leak_mb"] = len(_LEAK)


if int(os.getenv("INJECT_LEAK_MB", "0")):
    MAX_LEAK_MB = max(MAX_LEAK_MB, int(os.getenv("INJECT_LEAK_MB", "0")))
    apply_leak(int(os.getenv("INJECT_LEAK_MB", "0")))
    log.warning("started with injected memory leak of %s MB", FAULT["leak_mb"])
if FAULT["latency_ms"] or FAULT["error_rate"]:
    log.warning("started with injected fault: %s", FAULT)

# ---------------------------------------------------------------- metrics + middleware
REQS = Counter("shop_http_requests_total", "HTTP requests", ["service", "route", "status"])
LAT = Histogram(
    "shop_http_request_duration_seconds", "HTTP latency", ["service", "route"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)

http: httpx.AsyncClient = None  # set in startup


async def lifespan(_app):
    global http
    http = httpx.AsyncClient()
    tasks = []
    if ROLE == "storefront":
        tasks = [asyncio.create_task(poll_flags()), asyncio.create_task(refresh_stock_cache())]
    yield
    for t in tasks:
        t.cancel()
    await http.aclose()


app = FastAPI(title=f"SahajSale demo shop: {ROLE}", lifespan=lifespan)


@app.middleware("http")
async def chaos_and_metrics(request: Request, call_next):
    path = request.url.path
    exempt = path.startswith(("/admin", "/metrics", "/livez"))
    start = time.perf_counter()
    if not exempt:
        if FAULT["latency_ms"]:
            await asyncio.sleep(FAULT["latency_ms"] / 1000 * random.uniform(0.8, 1.2))
        if FAULT["error_rate"] and random.random() < FAULT["error_rate"]:
            log.error("injected fault: returning 500 for %s", path)
            resp = JSONResponse({"error": "injected_fault"}, status_code=500)
        else:
            resp = await call_next(request)
    else:
        resp = await call_next(request)
    if not exempt:
        dur = time.perf_counter() - start
        route = getattr(request.scope.get("route"), "path", path)
        REQS.labels(ROLE, route, str(resp.status_code)).inc()
        LAT.labels(ROLE, route).observe(dur)
        if dur > 1.0:
            log.warning("slow request: %s %s took %.0f ms", request.method, path, dur * 1000)
    return resp


@app.get("/livez")
async def livez():
    return {"ok": True}


@app.get("/ping")
async def ping():
    return {"service": ROLE, "ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/admin/fault")
async def get_fault():
    return {"service": ROLE, **FAULT}


@app.post("/admin/fault")
async def set_fault(request: Request):
    body = await request.json()
    if "latency_ms" in body:
        FAULT["latency_ms"] = int(body["latency_ms"])
    if "error_rate" in body:
        FAULT["error_rate"] = float(body["error_rate"])
    if "leak_mb" in body:
        apply_leak(int(body["leak_mb"]))
    log.warning("fault injected via admin API: %s", FAULT)
    return {"service": ROLE, **FAULT}


@app.delete("/admin/fault")
async def clear_fault():
    FAULT.update({"latency_ms": 0, "error_rate": 0.0})
    apply_leak(0)
    log.warning("fault cleared")
    return {"service": ROLE, **FAULT}


async def upstream(method: str, url: str, name: str, payload: dict = None):
    """Call a dependency. Returns (json, None) on success or (None, JSONResponse) on failure."""
    try:
        r = await http.request(method, url, json=payload, timeout=UP_TIMEOUT)
    except httpx.TimeoutException:
        log.error("timeout calling %s %s after %.1fs", name, url, UP_TIMEOUT)
        return None, JSONResponse({"error": "upstream_timeout", "upstream": name}, status_code=504)
    except httpx.HTTPError as exc:
        log.error("error calling %s: %s", name, exc)
        return None, JSONResponse({"error": "upstream_unreachable", "upstream": name}, status_code=502)
    if r.status_code >= 500:
        log.error("%s returned HTTP %s", name, r.status_code)
        return None, JSONResponse({"error": "upstream_error", "upstream": name}, status_code=502)
    if r.status_code >= 400:
        return None, JSONResponse(r.json(), status_code=r.status_code)
    return r.json(), None


# ================================================================= inventory
if ROLE == "inventory":
    STOCK = {sku: 500 for sku in PRODUCTS}

    @app.get("/stock/{sku}")
    async def stock(sku: str):
        if sku not in STOCK:
            return JSONResponse({"error": "unknown_sku"}, status_code=404)
        return {"sku": sku, "qty": STOCK[sku]}

    @app.post("/reserve")
    async def reserve(request: Request):
        body = await request.json()
        sku, qty = body.get("sku"), int(body.get("qty", 1))
        if sku not in STOCK:
            return JSONResponse({"error": "unknown_sku"}, status_code=404)
        if STOCK[sku] < qty:
            return JSONResponse({"error": "sold_out"}, status_code=409)
        STOCK[sku] -= qty
        if STOCK[sku] < 20:  # demo: auto-restock so the load test never runs dry
            STOCK[sku] = 500
        return {"ok": True, "sku": sku, "reserved": qty}


# ================================================================= cart
if ROLE == "cart":

    @app.post("/reserve")
    async def cart_reserve(request: Request):
        body = await request.json()
        data, err = await upstream("POST", f"{INVENTORY_URL}/reserve", "inventory", body)
        return err or data


# ================================================================= payments
if ROLE == "payments":

    @app.post("/charge")
    async def charge(request: Request):
        body = await request.json()
        return {"ok": True, "order_id": body.get("order_id"), "charged": body.get("amount", 0)}


# ================================================================= storefront
FLAGS = {"sale_mode": False, "urgency_banner_variant": "A"}
FLAGS_SOURCE = {"name": "none"}
STOCK_CACHE = {sku: 100 for sku in PRODUCTS}
RECONCILE: list = []
RESERVE_SEM = asyncio.Semaphore(5)
EXP = {"A": {"views": 0, "orders": 0}, "B": {"views": 0, "orders": 0}}
ORDERS = {"count": 0}


async def poll_flags():
    while True:
        try:
            if FLAGSMITH_ENV_KEY:
                r = await http.get(f"{FLAGSMITH_API}/flags/", headers={"X-Environment-Key": FLAGSMITH_ENV_KEY}, timeout=3)
                r.raise_for_status()
                for f in r.json():
                    name = f["feature"]["name"]
                    if name == "sale_mode":
                        FLAGS["sale_mode"] = bool(f["enabled"])
                    elif name == "urgency_banner_variant":
                        val = f.get("feature_state_value")
                        FLAGS["urgency_banner_variant"] = str(val) if f["enabled"] and val in ("A", "B") else "A"
                FLAGS_SOURCE["name"] = "flagsmith"
            else:
                r = await http.get(f"{SIDEKICK_URL}/api/mock-flags", timeout=2)
                data = r.json()
                FLAGS["sale_mode"] = bool(data.get("sale_mode", {}).get("enabled"))
                v = data.get("urgency_banner_variant", {})
                FLAGS["urgency_banner_variant"] = v.get("value", "A") if v.get("enabled") else "A"
                FLAGS_SOURCE["name"] = "mock"
        except Exception as exc:  # keep last known flags; never crash the shop over a flag read
            log.warning("flag poll failed (keeping last known values): %s", exc)
        await asyncio.sleep(2)


async def refresh_stock_cache():
    while True:
        for sku in PRODUCTS:
            try:
                r = await http.get(f"{INVENTORY_URL}/stock/{sku}", timeout=1)
                if r.status_code == 200:
                    STOCK_CACHE[sku] = r.json()["qty"]
            except Exception:
                pass
        await asyncio.sleep(15)


async def reserve_later(sku: str, qty: int, order_id: str):
    """sale_mode: reserve stock after the customer has paid, off the critical path."""
    if RESERVE_SEM.locked():
        RECONCILE.append({"order_id": order_id, "sku": sku, "qty": qty})
        return
    async with RESERVE_SEM:
        _, err = await upstream("POST", f"{CART_URL}/reserve", "cart", {"sku": sku, "qty": qty})
        if err:
            RECONCILE.append({"order_id": order_id, "sku": sku, "qty": qty})
            log.warning("deferred reservation failed for order %s; queued for reconciliation", order_id)


if ROLE == "storefront":

    @app.get("/api/products")
    async def products():
        return [{"sku": k, **v} for k, v in PRODUCTS.items()]

    @app.get("/api/state")
    async def state(view: int = 0):
        variant = FLAGS["urgency_banner_variant"]
        if view:
            EXP[variant]["views"] += 1
        return {
            "sale_mode": FLAGS["sale_mode"],
            "banner_variant": variant,
            "flags_source": FLAGS_SOURCE["name"],
            "reconcile_queue": len(RECONCILE),
            "orders": ORDERS["count"],
        }

    @app.get("/api/experiment")
    async def experiment():
        return EXP

    @app.get("/api/recommendations")
    async def recommendations():
        if FLAGS["sale_mode"]:
            return {"items": [], "disabled": True}
        results = await asyncio.gather(
            *[upstream("GET", f"{INVENTORY_URL}/stock/{sku}", "inventory") for sku in PRODUCTS]
        )
        for _, err in results:
            if err:
                return err
        _ = sum(i * i for i in range(120000))  # ranking work: the "heavy widget"
        items = sorted((d for d, _ in results), key=lambda d: d["qty"])[:3]
        return {"items": [{"sku": d["sku"], **PRODUCTS[d["sku"]], "left": d["qty"]} for d in items]}

    @app.post("/api/checkout")
    async def checkout(request: Request):
        started = time.perf_counter()
        body = await request.json()
        sku = body.get("sku", "diya-set")
        probe = bool(body.get("probe"))
        qty = 0 if probe else int(body.get("qty", 1))
        if sku not in PRODUCTS:
            return JSONResponse({"error": "unknown_sku"}, status_code=404)
        amount = PRODUCTS[sku]["price"] * qty
        order_id = uuid.uuid4().hex[:10]

        if FLAGS["sale_mode"]:
            if STOCK_CACHE.get(sku, 0) < qty:
                return JSONResponse({"error": "sold_out"}, status_code=409)
            STOCK_CACHE[sku] -= qty
            if qty:
                asyncio.create_task(reserve_later(sku, qty, order_id))
            mode = "sale_mode"
        else:
            _, err = await upstream("POST", f"{CART_URL}/reserve", "cart", {"sku": sku, "qty": qty})
            if err:
                return err
            mode = "live"

        _, err = await upstream("POST", f"{PAYMENTS_URL}/charge", "payments", {"order_id": order_id, "amount": amount})
        if err:
            return err

        if not probe:
            ORDERS["count"] += 1
            variant = body.get("variant")
            if variant in EXP:
                EXP[variant]["orders"] += 1
        return {
            "order_id": order_id,
            "mode": mode,
            "amount": amount,
            "ms": round((time.perf_counter() - started) * 1000),
        }

    PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Diya &amp; Co. – Festive Store</title>
<style>
:root{--ink:#3b1b0f;--cream:#fff7e6;--gold:#e8a317;--gold2:#f6c453;--maroon:#7a1f2b;--maroon2:#a3283a;--ok:#1f7a4d;--bad:#b3261e;--card:#fff}
*{box-sizing:border-box}
body{margin:0;color:var(--ink);background:radial-gradient(1200px 500px at 50% -120px,#ffe9b8,transparent),var(--cream);
 font-family:"Noto Sans Kannada","Kannada Sangam MN","Noto Sans Devanagari","Kohinoor Devanagari","Devanagari Sangam MN","Nirmala UI",system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}
header{position:sticky;top:0;z-index:5;background:linear-gradient(135deg,var(--maroon),var(--maroon2));color:#fff;padding:12px 20px;
 display:flex;justify-content:space-between;align-items:center;gap:12px;box-shadow:0 2px 12px rgba(0,0,0,.25)}
.brand{display:flex;align-items:center;gap:10px}.brand h1{margin:0;font-size:20px;line-height:1.1}.brand small{display:block;opacity:.8;font-size:12px}
.right{display:flex;align-items:center;gap:10px}
.chip{background:rgba(255,255,255,.16);padding:5px 12px;border-radius:99px;font-size:12px;white-space:nowrap;transition:.3s}
.chip.fast{background:var(--gold);color:#3b1b0f;font-weight:700;box-shadow:0 0 0 3px rgba(232,163,23,.35)}
.lang{display:flex;background:rgba(0,0,0,.25);border-radius:99px;padding:3px}
.lang button{border:0;background:transparent;color:#fff;padding:5px 12px;border-radius:99px;cursor:pointer;font:inherit;font-size:13px;line-height:1.3}
#l-kn{font-size:15px}
.lang button.on{background:#fff;color:var(--maroon);font-weight:700}
#banner{background:linear-gradient(90deg,var(--gold),var(--gold2),var(--gold));padding:10px 20px;text-align:center;font-weight:700}
.hero{max-width:980px;margin:0 auto;padding:30px 20px 4px;text-align:center}
.hero h2{margin:0;font-size:clamp(24px,4vw,36px);color:var(--maroon)}.hero p{margin:6px 0 0;color:#6b4a3a}
main{max-width:980px;margin:0 auto;padding:12px 20px 40px}
h3{margin:28px 0 12px;font-size:18px;color:var(--maroon)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.card{background:var(--card);border-radius:18px;overflow:hidden;box-shadow:0 2px 10px rgba(80,30,10,.1);text-align:center;transition:.2s;display:flex;flex-direction:column}
.card:hover{transform:translateY(-4px);box-shadow:0 10px 24px rgba(80,30,10,.18)}
.tile{font-size:56px;padding:22px 0;background:linear-gradient(135deg,#fff1cf,#ffd88a)}
.body{padding:14px 14px 16px;display:flex;flex-direction:column;gap:2px;flex:1}
.name{font-weight:600;min-height:3em;display:flex;align-items:center;justify-content:center}
.sub{font-size:12px;color:#8a6a5a;min-height:1.4em}.price{font-size:20px;font-weight:800;color:var(--maroon);margin-top:6px}
.card button{margin-top:10px;background:linear-gradient(135deg,var(--maroon),var(--maroon2));color:#fff;border:0;padding:10px 16px;border-radius:10px;cursor:pointer;font:inherit;font-weight:700}
.card button:active{transform:scale(.97)}.card button:disabled{opacity:.6;cursor:wait}
#rec{font-size:14px;color:#6b4a3a;background:#fff;border-radius:14px;padding:12px 16px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);padding:12px 20px;border-radius:12px;color:#fff;display:none;font-weight:700;box-shadow:0 6px 20px rgba(0,0,0,.3);max-width:90vw}
@media(max-width:520px){header{padding:10px 12px}.brand h1{white-space:nowrap;font-size:18px}.brand small{display:none}.chip{font-size:11px;padding:4px 9px}.lang button{padding:5px 9px}.hero{padding-top:20px}}
footer{text-align:center;color:#8a6a5a;font-size:12px;padding:0 20px 28px}
</style></head><body>
<header><div class="brand"><span style="font-size:28px">🪔</span><div><h1>Diya &amp; Co.</h1><small id="tag"></small></div></div>
<div class="right"><span class="chip" id="mode">…</span>
<div class="lang"><button id="l-en" onclick="setLang('en')">English</button><button id="l-kn" onclick="setLang('kn')">ಕನ್ನಡ</button><button id="l-hi" onclick="setLang('hi')">हिन्दी</button></div></div></header>
<div id="banner">…</div>
<div class="hero"><h2 id="hero"></h2><p id="herosub"></p></div>
<main><h3 id="h-picks"></h3><div class="grid" id="grid"></div>
<div id="recwrap"><h3 id="h-pop"></h3><div id="rec"></div></div></main>
<footer id="foot"></footer>
<div id="toast"></div>
<script>
let variant='A',lang='en',products=[],lastSale=false,lastRec=null;
const $=s=>document.querySelector(s);
const T={
 en:{tag:'Festive store',hero:'Light up your festival',herosub:'Handpicked festive favourites, delivered fast.',picks:'Festive picks',pop:'Popular right now',buy:'Buy now',
  A:'Festive offers end tonight – order now!',B:'Only a few left! Grab yours before they are gone.',sale:'⚡ Sale mode: fast checkout',std:'Standard mode',
  ok:'Order placed ✓',fail:'Checkout failed: ',net:'network error',left:'left',loading:'loading…',recno:'recommendations unavailable',foot:'Demo store for SahajSale'},
 kn:{tag:'ಹಬ್ಬದ ಅಂಗಡಿ',hero:'ನಿಮ್ಮ ಹಬ್ಬವನ್ನು ಬೆಳಗಿಸಿ',herosub:'ಆಯ್ದ ಹಬ್ಬದ ಉಡುಗೊರೆಗಳು, ವೇಗವಾಗಿ ನಿಮ್ಮ ಮನೆಗೆ.',picks:'ಹಬ್ಬದ ವಿಶೇಷ ಆಯ್ಕೆಗಳು',pop:'ಈಗ ಜನಪ್ರಿಯ',buy:'ಈಗಲೇ ಖರೀದಿಸಿ',
  A:'ಹಬ್ಬದ ಆಫರ್‌ಗಳು ಇಂದು ರಾತ್ರಿ ಮುಗಿಯುತ್ತವೆ – ಈಗಲೇ ಆರ್ಡರ್ ಮಾಡಿ!',B:'ಕೆಲವೇ ಉಳಿದಿವೆ! ಮುಗಿಯುವ ಮೊದಲು ನಿಮ್ಮದಾಗಿಸಿಕೊಳ್ಳಿ.',sale:'⚡ ಸೇಲ್ ಮೋಡ್: ವೇಗದ ಚೆಕ್‌ಔಟ್',std:'ಸಾಮಾನ್ಯ ಮೋಡ್',
  ok:'ಆರ್ಡರ್ ಆಯಿತು ✓',fail:'ಚೆಕ್‌ಔಟ್ ವಿಫಲವಾಗಿದೆ: ',net:'ನೆಟ್‌ವರ್ಕ್ ದೋಷ',left:'ಉಳಿದಿವೆ',loading:'ಲೋಡ್ ಆಗುತ್ತಿದೆ…',recno:'ಶಿಫಾರಸುಗಳು ಲಭ್ಯವಿಲ್ಲ',foot:'SahajSale ಗಾಗಿ ಡೆಮೊ ಅಂಗಡಿ'},
 hi:{tag:'त्योहारों की दुकान',hero:'अपना त्योहार रोशन करें',herosub:'चुनिंदा त्योहारी पसंद, तेज़ी से आपके घर तक।',picks:'त्योहार की खास पसंद',pop:'अभी लोकप्रिय',buy:'अभी खरीदें',
  A:'त्योहारी ऑफ़र आज रात खत्म – अभी ऑर्डर करें!',B:'बस कुछ ही बचे हैं! खत्म होने से पहले अपना लें।',sale:'⚡ सेल मोड: तेज़ चेकआउट',std:'सामान्य मोड',
  ok:'ऑर्डर हो गया ✓',fail:'चेकआउट विफल: ',net:'नेटवर्क त्रुटि',left:'बचे',loading:'लोड हो रहा है…',recno:'सुझाव उपलब्ध नहीं',foot:'SahajSale के लिए डेमो स्टोर'}};
const nm=x=>(lang==='kn'&&x.name_kn)||(lang==='hi'&&x.name_hi)||x.name;
const sub=x=>lang==='en'?(x.name_kn||''):x.name;   // English shown underneath the regional name
function paint(){const t=T[lang];document.documentElement.lang=lang;
 $('#tag').textContent=t.tag;$('#hero').textContent=t.hero;$('#herosub').textContent=t.herosub;$('#h-picks').textContent=t.picks;$('#h-pop').textContent=t.pop;$('#foot').textContent=t.foot;
 $('#banner').textContent=t[variant]||t.A;$('#mode').textContent=lastSale?t.sale:t.std;$('#mode').classList.toggle('fast',lastSale);
 $('#l-en').classList.toggle('on',lang==='en');$('#l-kn').classList.toggle('on',lang==='kn');$('#l-hi').classList.toggle('on',lang==='hi');
 $('#grid').innerHTML=products.map(x=>`<div class="card"><div class="tile">${x.emoji}</div><div class="body"><div class="name">${nm(x)}</div><div class="sub">${sub(x)}</div><div class="price">₹${x.price}</div><button onclick="buy('${x.sku}',this)">${t.buy}</button></div></div>`).join('');
 paintRec()}
function paintRec(){const t=T[lang],e=$('#rec');if(lastRec===null)e.textContent=t.loading;else if(lastRec===false)e.textContent=t.recno;
 else e.textContent=lastRec.map(i=>i.emoji+' '+nm(i)+' ('+i.left+' '+t.left+')').join('  ·  ')}
function setLang(l){lang=l;try{localStorage.setItem('lang',l)}catch(e){}paint()}
async function state(view){const s=await (await fetch('/api/state'+(view?'?view=1':''))).json();variant=s.banner_variant;lastSale=!!s.sale_mode;
 $('#recwrap').style.display=s.sale_mode?'none':'block';paint2()}
function paint2(){const t=T[lang];$('#banner').textContent=t[variant]||t.A;$('#mode').textContent=lastSale?t.sale:t.std;$('#mode').classList.toggle('fast',lastSale)}
function toast(m,ok){const e=$('#toast');e.textContent=m;e.style.background=ok?'var(--ok)':'var(--bad)';e.style.display='block';setTimeout(()=>e.style.display='none',3500)}
async function buy(sku,btn){const t=performance.now(),L=T[lang];btn&&(btn.disabled=true);
 try{const r=await fetch('/api/checkout',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sku,qty:1,variant})});
 const j=await r.json();const ms=Math.round(performance.now()-t);
 if(r.ok)toast(L.ok+' ('+ms+' ms)',true);else toast(L.fail+(j.error||r.status)+(j.upstream?' ('+j.upstream+')':''),false)}
 catch(e){toast(L.fail+L.net,false)}finally{btn&&(btn.disabled=false)}}
async function recs(){try{const r=await fetch('/api/recommendations');const j=await r.json();lastRec=r.ok?j.items:false;paintRec()}catch(e){}}
(async()=>{let saved=null;try{saved=localStorage.getItem('lang')}catch(e){}
 const q=new URLSearchParams(location.search).get('lang');
 lang=(q||saved||((l=>l.startsWith('kn')?'kn':l.startsWith('hi')?'hi':'en')((navigator.language||'').toLowerCase())));if(!T[lang])lang='en';
 products=await (await fetch('/api/products')).json();paint();
 await state(true);setInterval(()=>state(false),3000);recs();setInterval(recs,10000)})();
</script></body></html>"""

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return PAGE
