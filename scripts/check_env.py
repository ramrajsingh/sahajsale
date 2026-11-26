#!/usr/bin/env python3
"""Checks your .env against the real services. Read-only: nothing is flipped, nothing is changed.

    python3 scripts/check_env.py

Prints OK / PROBLEM per check. Secrets are never printed.
"""
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
problems = []


def load_env():
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        sys.exit("No .env file found in the project root.")
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))


def call(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"content-type": "application/json", **(headers or {})}
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=20)
        return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # DNS, TLS, proxy, timeout
        return None, f"{type(e).__name__}: {e}"


def report(ok, label, hint=""):
    print(("  OK       " if ok else "  PROBLEM  ") + label + (("  -> " + hint) if hint and not ok else ""))
    if not ok:
        problems.append(label)


load_env()
env, adm, pid = (os.environ.get(k, "") for k in ("FLAGSMITH_ENV_KEY", "FLAGSMITH_ADMIN_KEY", "FLAGSMITH_PROJECT_ID"))
print("Flagsmith")
if not (env and adm and (pid or os.environ.get("FLAGSMITH_FEATURE_IDS"))):
    report(False, "FLAGSMITH_ENV_KEY, FLAGSMITH_ADMIN_KEY and FLAGSMITH_PROJECT_ID are all set", "fill in all three in .env")
else:
    st, flags = call("GET", "https://edge.api.flagsmith.com/api/v1/flags/", {"X-Environment-Key": env})
    if st is None:
        report(False, "can reach Flagsmith", flags)
    else:
        report(st == 200, "environment key is accepted", "use the CLIENT-SIDE environment key of the right environment")
        if st == 200:
            names = {f["feature"]["name"]: f for f in flags}
            report("sale_mode" in names, "feature 'sale_mode' exists in this environment", "create a boolean feature named exactly sale_mode")
            report("urgency_banner_variant" in names, "feature 'urgency_banner_variant' exists", "create it, enabled, with string value A")
            if "sale_mode" in names:
                report(names["sale_mode"]["enabled"] is False, "sale_mode is currently OFF (the demo starts from OFF)", "switch it off before the demo")
        scheme, st = None, None
        for s in ("Api-Key", "Token"):  # organisation API key vs personal token: different Authorization schemes
            st, d = call("GET", f"https://api.flagsmith.com/api/v1/projects/{pid}/features/", {"Authorization": f"{s} {adm}"})
            if st == 200:
                scheme = s
                break
        report(st == 200, "admin key can read project " + (pid or "?") + (f" (key type: {'organisation API key' if scheme == 'Api-Key' else 'personal token'})" if scheme else ""),
               {401: "rejected both as Api-Key and as Token: the key is wrong, revoked, or from another account. Create an Organisation API key (Organisation settings > API keys)",
                403: "the key has no access to this project", 404: "project id not found: take it from the dashboard URL after /project/"}.get(st, str(d)))
        ADM_HDR = {"Authorization": f"{scheme or 'Api-Key'} {adm}"}
        st, d = call("GET", f"https://api.flagsmith.com/api/v1/environments/{env}/", ADM_HDR)
        if st == 200 and d:
            n = d.get("minimum_change_request_approvals")
            report(n in (None, 0), "change requests are OFF on this environment", "turn them off or the flag flip is refused")

print("Sarvam")
key = os.environ.get("SARVAM_API_KEY", "")
if not key:
    report(False, "SARVAM_API_KEY is set", "fill it in .env")
else:
    st, d = call("POST", "https://api.sarvam.ai/translate", {"api-subscription-key": key},
                 {"input": "Your store is slow.", "source_language_code": "en-IN", "target_language_code": "hi-IN", "model": os.environ.get("SARVAM_TRANSLATE_MODEL", "mayura:v1")})
    ok = st == 200 and isinstance(d, dict) and bool(d.get("translated_text"))
    report(ok, "key works (translated a test sentence to Hindi)", f"HTTP {st}" if st else str(d))
    st, d = call("POST", "https://api.sarvam.ai/text-to-speech", {"api-subscription-key": key},
                 {"text": "नमस्ते", "language_code": "hi-IN", "model": os.environ.get("SARVAM_TTS_MODEL", "bulbul:v3"),
                  "speaker": os.environ.get("SARVAM_TTS_SPEAKER", "priya")})
    ok = st == 200 and isinstance(d, dict) and bool(d.get("audios"))
    report(ok, "voice works (spoke a test word in Hindi)", f"HTTP {st}: {str(d)[:200]}" if st else str(d))

print()
print("ALL GOOD" if not problems else f"{len(problems)} problem(s) to fix, see above")
sys.exit(1 if problems else 0)
