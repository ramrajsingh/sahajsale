#!/usr/bin/env python3
"""Standalone festive-traffic generator (real checkouts against the storefront).

    python3 scripts/loadgen.py --url http://localhost:8001 --rps 8 --seconds 60

The dashboard's "Start festive traffic" button does the same thing and tallies lost revenue;
use this when you want load that is independent of the dashboard (e.g. from another machine).
"""
import argparse
import asyncio
import random
import time

import httpx

SKUS = ["diya-set", "rangoli-kit", "sweet-box", "silk-dupatta", "fairy-lights", "gift-hamper"]


async def main(url, rps, seconds):
    ok = fail = 0
    lat = []
    async with httpx.AsyncClient(timeout=5) as c:
        async def one():
            nonlocal ok, fail
            t = time.perf_counter()
            try:
                r = await c.post(f"{url}/api/checkout", json={"sku": random.choice(SKUS), "qty": 1})
                good = r.status_code == 200
            except Exception:
                good = False
            lat.append(time.perf_counter() - t)
            ok, fail = ok + good, fail + (not good)

        tasks, end = [], time.time() + seconds
        while time.time() < end:
            tasks.append(asyncio.create_task(one()))
            await asyncio.sleep(1 / rps)
        await asyncio.gather(*tasks)
    lat.sort()
    p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0
    print(f"orders ok={ok} failed={fail} success={100 * ok / max(1, ok + fail):.0f}% p95={p95 * 1000:.0f}ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8001")
    ap.add_argument("--rps", type=int, default=6)
    ap.add_argument("--seconds", type=int, default=60)
    a = ap.parse_args()
    asyncio.run(main(a.url, a.rps, a.seconds))
