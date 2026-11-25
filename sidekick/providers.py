"""Sponsor integrations for SahajSale: Sarvam AI (voice) and Flagsmith (flags).

Each integration has a live mode (keys present) and a clearly labelled mock mode, so the
demo still runs on stage if a key is missing or the network misbehaves. The dashboard
always shows which mode is active; nothing pretends to be live when it is not.
"""
import logging
import os
import re

import httpx

log = logging.getLogger("sahajsale.providers")

# ------------------------------------------------------------------ Sarvam AI
SARVAM_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_BASE = os.getenv("SARVAM_API_URL", "https://api.sarvam.ai").rstrip("/")
TRANSLATE_MODEL = os.getenv("SARVAM_TRANSLATE_MODEL", "mayura:v1")
TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "anushka")
STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v3")

LANGS = {
    "hi-IN": "हिन्दी", "ta-IN": "தமிழ்", "te-IN": "తెలుగు", "bn-IN": "বাংলা",
    "mr-IN": "मराठी", "gu-IN": "ગુજરાતી", "kn-IN": "ಕನ್ನಡ", "ml-IN": "മലയാളം", "en-IN": "English",
}


def sarvam_live() -> bool:
    return bool(SARVAM_KEY)


def _sarvam_headers() -> dict:
    return {"api-subscription-key": SARVAM_KEY}


# Offline Hindi templates, used only when there is no Sarvam key (English for other languages).
SERVICE_HI = {"inventory": "इन्वेंटरी", "cart": "कार्ट", "payments": "पेमेंट", "storefront": "स्टोर"}
MOCK_HI = {
    "alert": "नमस्ते! आपके स्टोर का चेकआउट अटक रहा है। वजह: {service_hi} सेवा बहुत धीमी है, इसलिए ऑर्डर पूरे नहीं हो पा रहे। "
             "मेरा सुझाव है कि सेल मोड चालू कर दें। इससे सुझाव बंद हो जाते हैं और ग्राहक आराम से भुगतान कर पाते हैं। "
             "मंज़ूरी के लिए हाँ कहें, रोकने के लिए नहीं कहें।",
    "resolved": "सेल मोड चालू हो गया है और चेकआउट फिर से ठीक चल रहा है। समस्या {secs} सेकंड में सुलझ गई।",
    "finops": "सेल के बाद सर्वर का खर्च घटाया जा सकता है। इससे हर महीने लगभग {amount} रुपये की बचत होगी।",
}


async def translate(client: httpx.AsyncClient, text_en: str, lang: str):
    """English -> target language. Returns (text, source, error_or_None)."""
    if lang == "en-IN":
        return text_en, "none", None
    if sarvam_live():
        try:
            r = await client.post(
                f"{SARVAM_BASE}/translate",
                headers=_sarvam_headers(),
                json={
                    "input": text_en,
                    "source_language_code": "en-IN",
                    "target_language_code": lang,
                    "model": TRANSLATE_MODEL,
                },
                timeout=15,
            )
            r.raise_for_status()
            return r.json()["translated_text"], "sarvam", None
        except Exception as exc:
            log.warning("Sarvam translate failed: %s", exc)
            return text_en, "fallback-en", f"Sarvam translate failed: {exc}"
    return text_en, "mock-en", None


async def tts(client: httpx.AsyncClient, text: str, lang: str):
    """Text -> speech. Returns (wav_bytes_or_None, mime, error_or_None). None => browser speech fallback."""
    if not sarvam_live():
        return None, None, None
    import base64

    try:
        r = await client.post(
            f"{SARVAM_BASE}/text-to-speech",
            headers=_sarvam_headers(),
            json={
                "text": text[:2400],
                "target_language_code": lang,
                "model": TTS_MODEL,
                "speaker": TTS_SPEAKER,
            },
            timeout=30,
        )
        r.raise_for_status()
        return base64.b64decode(r.json()["audios"][0]), "audio/wav", None
    except Exception as exc:
        log.warning("Sarvam TTS failed: %s", exc)
        return None, None, f"Sarvam TTS failed: {exc}"


async def stt(client: httpx.AsyncClient, audio: bytes, filename: str, content_type: str) -> str:
    """Speech -> text via Sarvam. Raises on failure."""
    if not sarvam_live():
        raise RuntimeError("Sarvam key not configured")
    r = await client.post(
        f"{SARVAM_BASE}/speech-to-text",
        headers=_sarvam_headers(),
        files={"file": (filename, audio, content_type)},
        data={"model": STT_MODEL},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json().get("transcript") or "").strip()


# ---- intent parsing for spoken/typed approvals (Hindi, English and other Indian languages)
_APPROVE_LATIN = {
    "haan", "han", "haa", "ha", "yes", "yeah", "yep", "ok", "okay", "approve", "approved", "thik", "theek",
    "karo", "kar", "chalo", "sahi", "ji", "aam", "sari", "sarai", "avunu", "sare", "hoy", "sure", "proceed",
}
_REJECT_LATIN = {"nahi", "nahin", "no", "nope", "ruko", "ruk", "mat", "stop", "cancel", "wait", "dont", "don't", "vendam", "vendaam"}
_APPROVE_INDIC = ["हाँ", "हां", "ठीक", "कर दो", "करो", "ஆம்", "சரி", "అవును", "సరే", "হ্যাঁ", "ঠিক", "હા", "ಹೌದು", "ಸರಿ", "അതെ", "ശരി", "होय"]
_REJECT_INDIC = ["नहीं", "नही", "रुको", "मत", "வேண்டாம்", "వద్దు", "না", "ના", "ಬೇಡ", "വേണ്ട", "नको"]


def parse_intent(text: str) -> str:
    """Returns 'approve', 'reject' or 'unclear'. Rejection wins when both appear."""
    t = (text or "").lower()
    tokens = set(re.findall(r"[a-z']+", t))
    if tokens & _REJECT_LATIN or any(w in t for w in _REJECT_INDIC):
        return "reject"
    if tokens & _APPROVE_LATIN or any(w in t for w in _APPROVE_INDIC):
        return "approve"
    return "unclear"


# ------------------------------------------------------------------ Flagsmith
FS_ENV = os.getenv("FLAGSMITH_ENV_KEY", "")
FS_ADMIN_KEY = os.getenv("FLAGSMITH_ADMIN_KEY", "")
FS_ADMIN_URL = os.getenv("FLAGSMITH_ADMIN_URL", "https://api.flagsmith.com/api/v1").rstrip("/")
FS_EDGE_URL = os.getenv("FLAGSMITH_API_URL", "https://edge.api.flagsmith.com/api/v1").rstrip("/")
FS_PROJECT = os.getenv("FLAGSMITH_PROJECT_ID", "")
FS_FEATURE_IDS = {
    k.strip(): int(v)
    for k, v in (p.split("=", 1) for p in os.getenv("FLAGSMITH_FEATURE_IDS", "").split(",") if "=" in p)
}


def flags_live() -> bool:
    return bool(FS_ENV and FS_ADMIN_KEY)


# Flagsmith has two kinds of admin credential that use different Authorization schemes:
#   Organisation API key (Organisation settings > API keys)  ->  "Api-Key <key>"
#   Personal token       (Account settings > Keys)           ->  "Token <key>"
# FLAGSMITH_ADMIN_AUTH pins one; otherwise the first write probes both and remembers the one that works.
FS_SCHEME = os.getenv("FLAGSMITH_ADMIN_AUTH", "").strip()


def _admin_headers() -> dict:
    return {"Authorization": f"{FS_SCHEME or 'Api-Key'} {FS_ADMIN_KEY}", "Content-Type": "application/json"}


async def _ensure_scheme(client: httpx.AsyncClient) -> None:
    global FS_SCHEME
    if FS_SCHEME:
        return
    for scheme in ("Api-Key", "Token"):
        try:
            r = await client.get(f"{FS_ADMIN_URL}/projects/", headers={"Authorization": f"{scheme} {FS_ADMIN_KEY}"}, timeout=10)
        except Exception:
            return  # network trouble: leave it unset; the real call below will surface the error
        if r.status_code not in (401, 403):
            FS_SCHEME = scheme
            log.info("Flagsmith admin auth scheme: %s", scheme)
            return


async def _feature_id(client: httpx.AsyncClient, name: str) -> int:
    if name in FS_FEATURE_IDS:
        return FS_FEATURE_IDS[name]
    if not FS_PROJECT:
        raise RuntimeError(f"set FLAGSMITH_FEATURE_IDS ({name}=<id>) or FLAGSMITH_PROJECT_ID so '{name}' can be resolved")
    r = await client.get(f"{FS_ADMIN_URL}/projects/{FS_PROJECT}/features/", params={"search": name}, headers=_admin_headers(), timeout=10)
    r.raise_for_status()
    for f in r.json().get("results", []):
        if f.get("name") == name:
            FS_FEATURE_IDS[name] = f["id"]
            return f["id"]
    raise RuntimeError(f"feature '{name}' not found in Flagsmith project {FS_PROJECT}")


async def read_flags(client: httpx.AsyncClient, mock_store: dict) -> dict:
    """Current flag values as {name: {enabled, value}}."""
    if not flags_live():
        return mock_store
    try:
        r = await client.get(f"{FS_EDGE_URL}/flags/", headers={"X-Environment-Key": FS_ENV}, timeout=4)
        r.raise_for_status()
        return {f["feature"]["name"]: {"enabled": f["enabled"], "value": f.get("feature_state_value")} for f in r.json()}
    except Exception as exc:
        log.warning("Flagsmith read failed: %s", exc)
        return mock_store


async def set_flag(client: httpx.AsyncClient, name: str, enabled: bool, mock_store: dict) -> str:
    """Flip a flag. Returns a human-readable note. Raises RuntimeError on failure (never silent)."""
    if not flags_live():
        mock_store.setdefault(name, {"value": None})["enabled"] = enabled
        return f"{name} set to {'ON' if enabled else 'OFF'} (mock flag store, Flagsmith keys not configured)"
    await _ensure_scheme(client)
    fid = await _feature_id(client, name)
    try:
        r = await client.get(f"{FS_ADMIN_URL}/environments/{FS_ENV}/featurestates/", params={"feature": fid}, headers=_admin_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        rows = data.get("results", data) if isinstance(data, dict) else data
        if not rows:
            raise RuntimeError("no feature state found for this environment")
        state_id = rows[0]["id"]
        r = await client.patch(
            f"{FS_ADMIN_URL}/environments/{FS_ENV}/featurestates/{state_id}/",
            headers=_admin_headers(), json={"enabled": enabled}, timeout=10,
        )
        r.raise_for_status()
        return f"{name} set to {'ON' if enabled else 'OFF'} in Flagsmith"
    except Exception as first:
        # Newer Flagsmith releases also expose a simpler environment-scoped endpoint; try it before giving up.
        try:
            url = FS_ADMIN_URL.replace("/api/v1", "/api/__future__") + f"/environments/{FS_ENV}/features/{fid}/"
            r = await client.patch(url, headers=_admin_headers(), json={"environment_default": {"enabled": enabled}}, timeout=10)
            r.raise_for_status()
            return f"{name} set to {'ON' if enabled else 'OFF'} in Flagsmith"
        except Exception as second:
            raise RuntimeError(f"Flagsmith write failed ({first}); fallback endpoint also failed ({second})")
