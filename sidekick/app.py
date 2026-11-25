"""SahajSale Sidekick: the owner-facing brain.

Flow:  detect (NudgeBee alert, or our own monitor as a fallback)
    -> investigate (NudgeBee's evidence-cited RCA; lightweight probe-based RCA otherwise)
    -> propose ONE safe, reversible action (turn on the Flagsmith `sale_mode` flag)
    -> tell the owner in their language (Sarvam translate + text-to-speech)
    -> approval by voice (Sarvam speech-to-text) or button
    -> execute (Flagsmith flag flip, tell NudgeBee's runbook the decision)
    -> verify real recovery with synthetic checkouts, then give a spoken summary.
"""
import asyncio
import hashlib
import logging
import os
import random
import statistics
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from sidekick import providers as P

logging.basicConfig(level=logging.INFO, format="%(asctime)s sidekick %(levelname)s %(message)s")
log = logging.getLogger("sahajsale")

URLS = {
    "storefront": os.getenv("STOREFRONT_URL", "http://localhost:8001"),
    "cart": os.getenv("CART_URL", "http://localhost:8002"),
    "inventory": os.getenv("INVENTORY_URL", "http://localhost:8003"),
    "payments": os.getenv("PAYMENTS_URL", "http://localhost:8004"),
}
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
NUDGEBEE_CALLBACK_TOKEN = os.getenv("NUDGEBEE_CALLBACK_TOKEN", "")
AUTO_DETECT = os.getenv("AUTO_DETECT", "1") == "1"
DEMO_CONTROLS = os.getenv("DEMO_CONTROLS", "1") == "1"
SLOW_MS = int(os.getenv("SLOW_MS", "1200"))
DETECT_CONSEC = int(os.getenv("DETECT_CONSEC", "2"))
COOLDOWN_S = 20
OPEN_STATES = ("investigating", "awaiting_approval", "executing", "verifying")

PRICES = {"diya-set": 399, "rangoli-kit": 249, "sweet-box": 799, "silk-dupatta": 1499, "fairy-lights": 349, "gift-hamper": 1999}

# ------------------------------------------------------------------ state
http: httpx.AsyncClient = None
LOCK = asyncio.Lock()
INCIDENTS: List[dict] = []
AUDIO: dict = {}  # (incident_id, kind) -> (bytes, mime)
MOCK_FLAGS = {"sale_mode": {"enabled": False, "value": None}, "urgency_banner_variant": {"enabled": True, "value": "A"}}
LANG = {"code": os.getenv("DEFAULT_LANG", "hi-IN")}
PROBES: deque = deque(maxlen=30)
PINGS = {n: deque(maxlen=6) for n in ("cart", "inventory", "payments")}
SERVICES = {n: {"ok": None, "ms": None} for n in ("storefront", "cart", "inventory", "payments")}
EXP_CACHE = {"data": {}}
LAST = {"nudgebee_seen": None, "callback": None, "bad_streak": 0, "good_streak": 0, "cooldown_until": 0.0, "flags_cache": (0.0, {})}
LOAD = {"on": False, "rps": 6}
LSTATS = {"ok": 0, "fail": 0, "rev_ok": 0, "rev_lost": 0}
FINOPS = {
    "source": "demo data (connect NudgeBee AI-FinOps to replace)",
    "findings": [
        {"resource": "inventory deployment", "issue": "Over-provisioned after sale scale-up (6 replicas, 20% CPU)", "fix": "Scale to 2 replicas", "monthly_saving_inr": 9200},
        {"resource": "storefront deployment", "issue": "CPU requests 4x actual usage", "fix": "Right-size CPU request 1000m -> 300m", "monthly_saving_inr": 6100},
        {"resource": "idle load-balancer + 2 unattached volumes", "issue": "Orphaned after festive campaign", "fix": "Delete unused resources", "monthly_saving_inr": 3100},
    ],
}


def now() -> float:
    return time.time()


def event(inc: dict, msg: str) -> None:
    inc["timeline"].append({"t": now(), "msg": msg})
    log.info("[%s] %s", inc["id"], msg)


def open_incident_obj() -> Optional[dict]:
    for inc in reversed(INCIDENTS):
        if inc["status"] in OPEN_STATES:
            return inc
    return None


def guess_service(text: str) -> str:
    t = (text or "").lower()
    for n in ("inventory", "payments", "cart", "storefront"):
        if n in t:
            return n
    return "backend"


# ------------------------------------------------------------------ voice composition
def english_text(kind: str, **kw) -> str:
    if kind == "alert":
        return (
            f"Namaste! Your store's checkout is failing. Cause: the {kw['service']} service is slow, so orders cannot complete. "
            "I suggest turning on Sale Mode. It pauses recommendations and uses saved stock counts, so customers can still pay. "
            "Say yes to approve, or no to cancel."
        )
    if kind == "resolved":
        return f"Sale Mode is on and checkout is working again. The problem was fixed in {kw['secs']} seconds."
    if kind == "finops":
        return f"After the sale, you can cut server costs. This saves about {kw['amount']} rupees every month."
    return ""


async def compose(kind: str, lang: str, **kw):
    """Returns dict(text_local, text_en, text_source, audio(bytes|None), mime, audio_source, errors[])."""
    en = english_text(kind, **kw)
    errors = []
    if P.sarvam_live() or lang == "en-IN":
        local, src, err = await P.translate(http, en, lang)
        if err:
            errors.append(err)
    elif lang == "hi-IN":
        local, src = P.MOCK_HI[kind].format(service_hi=P.SERVICE_HI.get(kw.get("service"), "बैकएंड"), **kw), "mock-template"
    else:
        local, src = en, "mock-en"
    audio, mime, err = await P.tts(http, local, lang)
    if err:
        errors.append(err)
    return {
        "lang": lang, "text_local": local, "text_en": en, "text_source": src,
        "has_audio": audio is not None, "mime": mime,
        "audio_source": "sarvam" if audio else "browser-speech (no Sarvam key)" if not P.sarvam_live() else "browser-speech (Sarvam TTS failed)",
        "errors": errors,
    }, audio


async def attach_voice(inc: dict, kind: str, lang: str, **kw) -> None:
    voice, audio = await compose(kind, lang, **kw)
    key = "voice" if kind == "alert" else "summary_voice"
    inc[key] = voice
    if audio:
        AUDIO[(inc["id"], kind)] = (audio, voice["mime"])
    else:
        AUDIO.pop((inc["id"], kind), None)
    for e in voice["errors"]:
        event(inc, e)


# ------------------------------------------------------------------ probes + monitor
async def synthetic_checkout() -> dict:
    t = time.perf_counter()
    try:
        r = await http.post(f"{URLS['storefront']}/api/checkout", json={"sku": "diya-set", "probe": True}, timeout=4)
        ok = r.status_code == 200
    except Exception:
        ok = False
    return {"ok": ok, "ms": round((time.perf_counter() - t) * 1000), "t": now()}


async def ping_service(name: str) -> dict:
    t = time.perf_counter()
    try:
        r = await http.get(f"{URLS[name]}/ping", timeout=3)
        ok = r.status_code == 200
    except Exception:
        ok = False
    return {"ok": ok, "ms": round((time.perf_counter() - t) * 1000)}


def probe_is_bad(p: dict) -> bool:
    return (not p["ok"]) or p["ms"] > SLOW_MS


def build_fallback_rca() -> dict:
    scores, evidence = {}, []
    for n in ("cart", "inventory", "payments"):
        recent = list(PINGS[n])[-3:]
        fails = sum(1 for p in recent if not p["ok"])
        avg = sum(p["ms"] for p in recent) / max(1, len(recent))
        scores[n] = fails * 5000 + avg
        evidence.append(f"{n}: avg ping {avg:.0f} ms, {fails}/{len(recent)} failed pings")
    root = max(scores, key=scores.get)
    if scores[root] < 200:
        root = "storefront"
        summary = "Checkout is failing but no single dependency stands out (all pings healthy)."
    else:
        summary = f"{root} is the slowest dependency in the checkout path; cart and storefront are slow only because they wait on it."
    recent = list(PROBES)[-3:]
    evidence.append("synthetic checkouts: " + ", ".join(f"{'ok' if p['ok'] else 'FAIL'} {p['ms']}ms" for p in recent))
    return {"source": "sahajsale-monitor (fallback probes)", "summary": summary, "root_cause_service": root, "evidence": evidence}


async def create_incident(title: str, severity: str, rca: dict, callback_url: Optional[str] = None) -> dict:
    inc = {
        "id": uuid.uuid4().hex[:8], "created": now(), "title": title, "severity": severity, "status": "investigating",
        "rca": rca, "proposed_action": None, "voice": None, "summary_voice": None, "approval": None,
        "callback_url": callback_url, "recovery": None, "timeline": [],
    }
    INCIDENTS.append(inc)
    event(inc, f"Incident opened ({rca['source']})")
    event(inc, f"Root cause: {rca['summary']}")
    await propose(inc)
    return inc


async def propose(inc: dict) -> None:
    flags = await P.read_flags(http, MOCK_FLAGS)
    if flags.get("sale_mode", {}).get("enabled"):
        inc["status"] = "needs_human"
        event(inc, "Sale Mode is already on, so there is no safe automatic action left. A person needs to look (consider a rollback).")
        return
    inc["proposed_action"] = {
        "type": "enable_flag", "flag": "sale_mode",
        "description": "Turn on Sale Mode (Flagsmith flag): pause recommendations, use cached stock, reserve stock after payment. Instant and reversible.",
    }
    await attach_voice(inc, "alert", LANG["code"], service=inc["rca"].get("root_cause_service", "backend"))
    inc["status"] = "awaiting_approval"
    event(inc, f"Voice alert ready in {inc['voice']['lang']} ({inc['voice']['audio_source']}). Waiting for the owner's approval.")


async def monitor_loop():
    while True:
        try:
            probe, *pings = await asyncio.gather(synthetic_checkout(), *[ping_service(n) for n in PINGS])
            PROBES.append(probe)
            SERVICES["storefront"] = {"ok": not probe_is_bad(probe), "ms": probe["ms"]}
            for n, p in zip(PINGS, pings):
                PINGS[n].append(p)
                SERVICES[n] = {"ok": p["ok"] and p["ms"] <= SLOW_MS, "ms": p["ms"]}
            if probe_is_bad(probe):
                LAST["bad_streak"] += 1
                LAST["good_streak"] = 0
            else:
                LAST["good_streak"] += 1
                LAST["bad_streak"] = 0
            try:
                r = await http.get(f"{URLS['storefront']}/api/experiment", timeout=2)
                EXP_CACHE["data"] = r.json()
            except Exception:
                pass
            async with LOCK:
                inc = open_incident_obj()
                if AUTO_DETECT and LAST["bad_streak"] >= DETECT_CONSEC and not inc and now() > LAST["cooldown_until"]:
                    await create_incident("Checkout is failing or very slow", "critical", build_fallback_rca())
                elif inc and inc["status"] == "awaiting_approval" and LAST["good_streak"] >= 6 and inc["rca"]["source"].startswith("sahajsale"):
                    inc["status"] = "self_recovered"
                    event(inc, "Checkout recovered on its own; no action needed.")
        except Exception as exc:
            log.exception("monitor error: %s", exc)
        await asyncio.sleep(2)


async def one_order():
    sku = random.choice(list(PRICES))
    try:
        r = await http.post(f"{URLS['storefront']}/api/checkout", json={"sku": sku, "qty": 1}, timeout=4)
        ok = r.status_code == 200
    except Exception:
        ok = False
    if ok:
        LSTATS["ok"] += 1
        LSTATS["rev_ok"] += PRICES[sku]
    else:
        LSTATS["fail"] += 1
        LSTATS["rev_lost"] += PRICES[sku]


async def load_loop():
    while True:
        if not LOAD["on"]:
            await asyncio.sleep(0.5)
            continue
        asyncio.create_task(one_order())
        await asyncio.sleep(1 / max(1, LOAD["rps"]))


@asynccontextmanager
async def lifespan(_app):
    global http
    http = httpx.AsyncClient()
    tasks = [asyncio.create_task(monitor_loop()), asyncio.create_task(load_loop())]
    yield
    for t in tasks:
        t.cancel()
    await http.aclose()


app = FastAPI(title="SahajSale Sidekick", lifespan=lifespan)
STATIC = Path(__file__).parent / "static"


def check_token(request: Request) -> None:
    if not WEBHOOK_TOKEN:
        return
    supplied = request.headers.get("x-webhook-token") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if supplied != WEBHOOK_TOKEN:
        raise HTTPException(401, "bad webhook token")


# ------------------------------------------------------------------ decisions + execution
async def notify_nudgebee(inc: dict, decision: str) -> None:
    url = inc.get("callback_url")
    if not url:
        return
    headers = {"Authorization": f"Bearer {NUDGEBEE_CALLBACK_TOKEN}"} if NUDGEBEE_CALLBACK_TOKEN else {}
    try:
        r = await http.post(url, json={
            "incident_id": inc["id"], "decision": decision, "status": inc["status"],
            "approved_by": (inc.get("approval") or {}).get("by"), "at": now(),
        }, headers=headers, timeout=8)
        event(inc, f"Told NudgeBee runbook: {decision} (HTTP {r.status_code})")
    except Exception as exc:
        event(inc, f"Could not reach NudgeBee callback: {exc}")


async def decide(inc: dict, decision: str, by: str, transcript: Optional[str] = None) -> dict:
    async with LOCK:
        if inc["status"] != "awaiting_approval":
            raise HTTPException(409, f"incident is '{inc['status']}', not awaiting approval")
        inc["approval"] = {"decision": decision, "by": by, "transcript": transcript, "at": now()}
        if decision == "reject":
            inc["status"] = "rejected"
            event(inc, f"Owner rejected the action ({by}). Nothing was changed.")
            LAST["cooldown_until"] = now() + COOLDOWN_S
        else:
            inc["status"] = "executing"
            event(inc, f"Owner approved by {by}" + (f': "{transcript}"' if transcript else ""))
    if decision == "reject":
        await notify_nudgebee(inc, "rejected")
    else:
        asyncio.create_task(execute(inc))
    return inc


def probe_stats(probes: list) -> dict:
    if not probes:
        return {"success_pct": None, "median_ms": None}
    return {
        "success_pct": round(100 * sum(1 for p in probes if p["ok"]) / len(probes)),
        "median_ms": round(statistics.median(p["ms"] for p in probes)),
    }


def before_probes() -> list:
    """The run of failing probes that triggered the incident (not healthy ones from before the break)."""
    tail = []
    for p in reversed(PROBES):
        if not probe_is_bad(p):
            break
        tail.append(p)
    return tail if len(tail) >= 2 else list(PROBES)[-5:]


async def execute(inc: dict) -> None:
    before = probe_stats(before_probes())
    try:
        note = await P.set_flag(http, "sale_mode", True, MOCK_FLAGS)
        event(inc, note)
    except Exception as exc:
        inc["status"] = "failed"
        event(inc, f"Could not change the flag: {exc}")
        await notify_nudgebee(inc, "failed")
        return
    await notify_nudgebee(inc, "approved")
    inc["status"] = "verifying"
    event(inc, "Verifying with live test checkouts...")
    good, deadline, after = 0, time.time() + 60, []
    while time.time() < deadline:
        p = await synthetic_checkout()
        after.append(p)
        good = good + 1 if not probe_is_bad(p) else 0
        if good >= 3:
            break
        await asyncio.sleep(1.5)
    secs = round(now() - inc["created"])
    inc["recovery"] = {"before": before, "after": probe_stats(after[-3:]), "seconds_to_recover": secs}
    if good >= 3:
        inc["status"] = "resolved"
        event(inc, f"Checkout is healthy again ({inc['recovery']['after']['median_ms']} ms median). Resolved in {secs}s.")
        LAST["cooldown_until"] = now() + COOLDOWN_S
        await attach_voice(inc, "resolved", LANG["code"], secs=secs)
    else:
        inc["status"] = "failed"
        event(inc, "Sale Mode was on but checkout is still unhealthy. Escalate: consider a rollback of the last deploy.")


# ------------------------------------------------------------------ API: incidents
@app.get("/api/mock-flags")
async def mock_flags():
    return MOCK_FLAGS


def find(incident_id: str) -> dict:
    for inc in INCIDENTS:
        if inc["id"] == incident_id:
            return inc
    raise HTTPException(404, "no such incident")


@app.post("/api/incidents/{incident_id}/approve")
async def approve(incident_id: str):
    return await decide(find(incident_id), "approve", "button")


@app.post("/api/incidents/{incident_id}/reject")
async def reject(incident_id: str):
    return await decide(find(incident_id), "reject", "button")


@app.post("/api/incidents/{incident_id}/voice-approve")
async def voice_approve(incident_id: str, audio: Optional[UploadFile] = File(None), transcript: str = Form("")):
    inc = find(incident_id)
    by = "typed reply"
    if audio is not None:
        raw = await audio.read()
        try:
            transcript = await P.stt(http, raw, audio.filename or "reply.wav", audio.content_type or "audio/wav")
            by = "voice (Sarvam speech-to-text)"
        except Exception as exc:
            if not transcript:
                raise HTTPException(503, f"Voice transcription unavailable: {exc}. Type your reply instead.")
    if not transcript:
        raise HTTPException(400, "no transcript")
    intent = P.parse_intent(transcript)
    if intent == "unclear":
        event(inc, f'Could not tell yes from no in "{transcript}". Asking again; nothing was changed.')
        return {"intent": "unclear", "transcript": transcript, "status": inc["status"]}
    await decide(inc, intent, by, transcript)
    return {"intent": intent, "transcript": transcript, "status": inc["status"]}


@app.get("/api/incidents/{incident_id}/audio")
async def incident_audio(incident_id: str, kind: str = "alert"):
    data = AUDIO.get((incident_id, kind))
    if not data:
        raise HTTPException(404, "no audio (browser speech fallback is used)")
    return Response(content=data[0], media_type=data[1])


class LangBody(BaseModel):
    lang: str
    revoice: bool = True


@app.post("/api/lang")
async def set_lang(body: LangBody):
    if body.lang not in P.LANGS:
        raise HTTPException(400, "unsupported language")
    LANG["code"] = body.lang
    inc = open_incident_obj()
    if body.revoice and inc and inc["status"] == "awaiting_approval":
        await attach_voice(inc, "alert", body.lang, service=inc["rca"].get("root_cause_service", "backend"))
        event(inc, f"Voice alert re-recorded in {P.LANGS[body.lang]}")
    return {"lang": LANG["code"]}


# ------------------------------------------------------------------ API: webhooks from NudgeBee
class NudgeBeeAlert(BaseModel):
    title: str = "Incident reported by NudgeBee"
    severity: str = "critical"
    summary: str = ""
    root_cause: Optional[str] = None
    service: Optional[str] = None
    evidence: List[str] = []
    callback_url: Optional[str] = None


@app.post("/api/webhooks/nudgebee")
async def nudgebee_webhook(alert: NudgeBeeAlert, request: Request):
    """Called by a NudgeBee runbook step after AI-SRE has investigated an incident."""
    check_token(request)
    LAST["nudgebee_seen"] = now()
    root = alert.service or guess_service(alert.root_cause or alert.summary or alert.title)
    rca = {
        "source": "NudgeBee AI-SRE", "root_cause_service": root, "evidence": alert.evidence,
        "summary": alert.root_cause or alert.summary or alert.title,
    }
    async with LOCK:
        inc = open_incident_obj()
        if inc and inc["status"] in ("investigating", "awaiting_approval"):
            inc["rca"] = rca
            inc["callback_url"] = alert.callback_url or inc.get("callback_url")
            inc["title"] = alert.title
            event(inc, "NudgeBee's evidence-cited root cause replaced our quick probe-based one")
            if inc["status"] == "awaiting_approval":
                await attach_voice(inc, "alert", LANG["code"], service=root)
            return {"incident_id": inc["id"], "status": inc["status"], "merged": True}
        inc = await create_incident(alert.title, alert.severity, rca, alert.callback_url)
    return {"incident_id": inc["id"], "status": inc["status"], "merged": False}


class Finding(BaseModel):
    resource: str
    issue: str = ""
    fix: str = ""
    monthly_saving_inr: int = 0


class FinopsBody(BaseModel):
    findings: List[Finding]


@app.post("/api/webhooks/finops")
async def finops_webhook(body: FinopsBody, request: Request):
    """Called by a NudgeBee AI-FinOps runbook step with its waste findings."""
    check_token(request)
    LAST["nudgebee_seen"] = now()
    FINOPS["source"] = "NudgeBee AI-FinOps"
    FINOPS["findings"] = [f.model_dump() for f in body.findings]
    return {"total_monthly_saving_inr": sum(f.monthly_saving_inr for f in body.findings)}


@app.get("/api/finops")
async def finops(lang: Optional[str] = None):
    total = sum(f["monthly_saving_inr"] for f in FINOPS["findings"])
    voice, audio = await compose("finops", lang or LANG["code"], amount=f"{total:,}")
    key = hashlib.md5(f"{voice['text_local']}{voice['lang']}".encode()).hexdigest()[:8]
    if audio:
        AUDIO[("finops-" + key, "finops")] = (audio, voice["mime"])
    return {**FINOPS, "total_monthly_saving_inr": total, "voice": voice, "audio_id": ("finops-" + key) if audio else None}


@app.post("/api/callbacks/echo")
async def callback_echo(request: Request):
    """Local stand-in for a NudgeBee runbook's callback endpoint (used by tests and demos)."""
    LAST["callback"] = await request.json()
    return {"ok": True}


# ------------------------------------------------------------------ API: state for the dashboard
@app.get("/api/state")
async def state():
    ts, cached = LAST["flags_cache"]
    if now() - ts > 3:
        cached = await P.read_flags(http, MOCK_FLAGS)
        LAST["flags_cache"] = (now(), cached)
    shown = []
    for inc in reversed(INCIDENTS[-5:]):
        shown.append({k: v for k, v in inc.items()})
    return {
        "now": now(), "lang": LANG["code"], "langs": P.LANGS,
        "mode": {
            "flags": "flagsmith" if P.flags_live() else "mock",
            "voice": "sarvam" if P.sarvam_live() else "mock",
            "nudgebee_last_seen": LAST["nudgebee_seen"],
            "auto_detect": AUTO_DETECT,
            "demo_controls": DEMO_CONTROLS,
        },
        "services": SERVICES,
        "probes": [{"ok": p["ok"], "ms": p["ms"]} for p in list(PROBES)[-20:]],
        "flags": cached,
        "incidents": shown,
        "load": {**LOAD, **LSTATS},
        "experiment": EXP_CACHE["data"],
        "last_callback": LAST["callback"],
    }


# ------------------------------------------------------------------ demo controls
def need_demo() -> None:
    if not DEMO_CONTROLS:
        raise HTTPException(403, "demo controls are disabled")


class BreakBody(BaseModel):
    service: str = "inventory"
    latency_ms: int = 3000
    error_rate: float = 0.0
    leak_mb: int = 0


@app.post("/api/demo/break")
async def demo_break(body: BreakBody):
    need_demo()
    if body.service not in URLS:
        raise HTTPException(400, "unknown service")
    r = await http.post(f"{URLS[body.service]}/admin/fault", json=body.model_dump(exclude={"service"}), timeout=5)
    return r.json()


@app.post("/api/demo/heal")
async def demo_heal():
    need_demo()
    for name, url in URLS.items():
        try:
            await http.delete(f"{url}/admin/fault", timeout=5)
        except Exception as exc:
            log.warning("could not clear fault on %s: %s", name, exc)
    note = await P.set_flag(http, "sale_mode", False, MOCK_FLAGS)
    LAST["cooldown_until"] = now() + COOLDOWN_S
    return {"ok": True, "flag": note}


class LoadBody(BaseModel):
    on: bool
    rps: int = 6


@app.post("/api/demo/load")
async def demo_load(body: LoadBody):
    need_demo()
    LOAD.update({"on": body.on, "rps": max(1, min(body.rps, 25))})
    return LOAD


@app.post("/api/demo/reset")
async def demo_reset():
    need_demo()
    LSTATS.update({"ok": 0, "fail": 0, "rev_ok": 0, "rev_lost": 0})
    INCIDENTS.clear()
    AUDIO.clear()
    LAST["cooldown_until"] = 0.0
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
