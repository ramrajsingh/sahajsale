#!/usr/bin/env python3
"""Break and heal the demo shop on purpose.

Local / docker compose (talks to the Sidekick's demo API):
    python3 scripts/chaos.py break            # inventory: 3 s latency + 10% errors
    python3 scripts/chaos.py heal

Kubernetes (changes the real Deployment, so NudgeBee sees a genuine rollout and failing pods):
    python3 scripts/chaos.py bad-deploy       # inventory rollout with injected latency + errors (a "bad release")
    python3 scripts/chaos.py oom              # inventory rollout that leaks memory past its 256Mi limit -> OOMKilled
    python3 scripts/chaos.py rollback         # kubectl rollout undo
    python3 scripts/chaos.py heal-k8s         # remove the injected env vars
"""
import json
import os
import subprocess
import sys
import urllib.request

SK = os.getenv("SAHAJSALE_URL", "http://localhost:8000")
NS = os.getenv("NAMESPACE", "sahajsale")


def api(path, body=None):
    req = urllib.request.Request(SK + path, data=json.dumps(body or {}).encode(), headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def kubectl(*args):
    cmd = ["kubectl", "-n", NS, *args]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "break":
        print(api("/api/demo/break", {"service": "inventory", "latency_ms": 3000, "error_rate": 0.1}))
    elif cmd == "heal":
        print(api("/api/demo/heal"))
    elif cmd == "bad-deploy":
        kubectl("set", "env", "deploy/inventory", "INJECT_LATENCY_MS=3000", "INJECT_ERROR_RATE=0.1")
    elif cmd == "oom":
        kubectl("set", "env", "deploy/inventory", "INJECT_LEAK_MB=400")
    elif cmd == "rollback":
        kubectl("rollout", "undo", "deploy/inventory")
    elif cmd == "heal-k8s":
        kubectl("set", "env", "deploy/inventory", "INJECT_LATENCY_MS-", "INJECT_ERROR_RATE-", "INJECT_LEAK_MB-")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
