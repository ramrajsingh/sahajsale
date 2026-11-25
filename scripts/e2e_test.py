#!/usr/bin/env python3
"""End-to-end test of the whole SahajSale story, in mock mode (no sponsor keys needed).

  break inventory -> checkout fails -> Sidekick detects + finds root cause -> voice alert
  -> owner says "haan" -> Sale Mode flag flips -> checkout recovers even though inventory is still broken
  Then: NudgeBee webhook path (with callback), rejection, FinOps webhook.

Run from the project root:  python3 scripts/e2e_test.py
"""
import os
import signal
import subprocess
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SK, ST = "http://localhost:8000", "http://localhost:8001"
failures = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        failures.append(label)


def wait_for(fn, timeout, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(every)
    return None


def state(c):
    return c.get(f"{SK}/api/state").json()


def latest(c, status=None):
    inc = state(c)["incidents"]
    if inc and (status is None or inc[0]["status"] == status):
        return inc[0]
    return None


def main():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SARVAM", "FLAGSMITH"))}
    env.update({"DEFAULT_LANG": "hi-IN", "DETECT_CONSEC": "2"})
    proc = subprocess.Popen(["bash", os.path.join(ROOT, "scripts/run_local.sh")], cwd=ROOT, env=env,
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(timeout=10) as c:
            print("1. Stack starts and is healthy")
            check(wait_for(lambda: state(c)["services"]["storefront"]["ok"], 30), "storefront checkout probe is healthy")
            r = c.post(f"{ST}/api/checkout", json={"sku": "diya-set", "qty": 1})
            check(r.status_code == 200 and r.json()["mode"] == "live", "real checkout works in live mode")
            check(c.get(ST).status_code == 200 and "Diya" in c.get(ST).text, "storefront page serves")
            check(b"shop_http_requests_total" in c.get("http://localhost:8003/metrics").content, "Prometheus metrics exposed")

            print("2. Festive traffic + broken inventory")
            c.post(f"{SK}/api/demo/reset")
            c.post(f"{SK}/api/demo/load", json={"on": True, "rps": 5})
            time.sleep(2)
            c.post(f"{SK}/api/demo/break", json={"service": "inventory", "latency_ms": 3000, "error_rate": 0.1})
            r = c.post(f"{ST}/api/checkout", json={"sku": "diya-set", "qty": 1}, timeout=10)
            check(r.status_code in (502, 504), f"checkout now fails ({r.status_code} {r.json().get('upstream')})")

            print("3. Sidekick detects, finds root cause, speaks")
            inc = wait_for(lambda: latest(c, "awaiting_approval"), 45)
            check(inc is not None, "incident reaches awaiting_approval")
            if not inc:
                return
            check(inc["rca"]["root_cause_service"] == "inventory", f"root cause = {inc['rca']['root_cause_service']}")
            check(bool(inc["voice"]["text_local"]) and inc["voice"]["lang"] == "hi-IN", "Hindi voice alert composed")
            check(any("ऀ" <= ch <= "ॿ" for ch in inc["voice"]["text_local"]), "alert text is in Devanagari")
            check(state(c)["load"]["fail"] > 0, f"orders were being lost ({state(c)['load']['fail']} failed)")

            print("4. Approval: unclear first, then voice-style 'haan'")
            r = c.post(f"{SK}/api/incidents/{inc['id']}/voice-approve", data={"transcript": "hmm let me think"}).json()
            check(r["intent"] == "unclear" and r["status"] == "awaiting_approval", "unclear reply changes nothing")
            r = c.post(f"{SK}/api/incidents/{inc['id']}/voice-approve", data={"transcript": "haan kar do"}).json()
            check(r["intent"] == "approve", "'haan kar do' understood as approve")

            print("5. Flag flips and checkout recovers while inventory is STILL broken")
            inc = wait_for(lambda: latest(c, "resolved"), 60)
            check(inc is not None, "incident reaches resolved")
            st = state(c)
            check(st["flags"]["sale_mode"]["enabled"] is True, "sale_mode flag is ON")
            check(c.get("http://localhost:8003/admin/fault").json()["latency_ms"] == 3000, "inventory is still broken (3000 ms)")
            t = time.time()
            r = c.post(f"{ST}/api/checkout", json={"sku": "diya-set", "qty": 1})
            check(r.status_code == 200 and r.json()["mode"] == "sale_mode" and time.time() - t < 1.2, f"checkout works in sale mode ({r.json().get('ms')} ms)")
            if inc and inc["recovery"]:
                rec = inc["recovery"]
                check(rec["after"]["success_pct"] == 100, f"recovery verified: {rec}")
                check(rec["before"]["success_pct"] <= 50 and rec["before"]["median_ms"] > 1200, "'before' numbers describe the outage, not healthy probes")
            check(inc is not None and inc["summary_voice"] is not None, "spoken summary composed")
            check(c.get(f"{ST}/api/state").json()["reconcile_queue"] >= 0, "deferred reservations tracked")
            c.post(f"{SK}/api/demo/load", json={"on": False})

            print("6. NudgeBee webhook path, with runbook callback, and rejection")
            c.post(f"{SK}/api/demo/heal")
            time.sleep(1)
            r = c.post(f"{SK}/api/webhooks/nudgebee", json={
                "title": "Checkout 5xx spike", "service": "inventory",
                "root_cause": "inventory-service latency regression after deploy v42",
                "evidence": ["p95 latency 3.1s (baseline 40ms)", "cart timeouts calling inventory"],
                "callback_url": f"{SK}/api/callbacks/echo"}).json()
            check(r["status"] == "awaiting_approval", "NudgeBee alert opens an incident awaiting approval")
            check(r["incident_id"] and state(c)["incidents"][0]["rca"]["source"] == "NudgeBee AI-SRE", "RCA is attributed to NudgeBee AI-SRE")
            r2 = c.post(f"{SK}/api/incidents/{r['incident_id']}/voice-approve", data={"transcript": "नहीं रुको"}).json()
            check(r2["intent"] == "reject", "Hindi 'नहीं रुको' understood as reject")
            cb = wait_for(lambda: state(c)["last_callback"], 5)
            check(bool(cb) and cb["decision"] == "rejected", f"NudgeBee callback received: {cb and cb['decision']}")

            print("7. FinOps webhook")
            r = c.post(f"{SK}/api/webhooks/finops", json={"findings": [{"resource": "inventory", "fix": "scale 6->2", "monthly_saving_inr": 5000}]}).json()
            check(r["total_monthly_saving_inr"] == 5000, "FinOps findings accepted")
            f = c.get(f"{SK}/api/finops").json()
            check("5,000" in f["voice"]["text_en"] and f["source"] == "NudgeBee AI-FinOps", "FinOps voice summary uses the new total")
            r = c.post(f"{SK}/api/lang", json={"lang": "ta-IN"})
            check(r.status_code == 200, "language switch accepted")
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
