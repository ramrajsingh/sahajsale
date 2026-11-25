# सहज सेल · SahajSale

**A sale-day sidekick for small online sellers.** It watches your store during Diwali-to-New-Year traffic, finds out *why* checkout is breaking, and tells you in your own language. You approve with one spoken word ("haan"), and it fixes the problem with a safe, reversible switch, then proves checkout is healthy again.

Built for **Festive Hacks × WeAreDevelopers Conference India**. It uses all three sponsors' tools, and each one does real work:

| Sponsor | What it does in SahajSale |
|---|---|
| **NudgeBee** | AI-SRE investigates the incident to an evidence-cited root cause and runs the approval-gated runbook. AI-FinOps finds the waste to cut once the sale ends. |
| **Sarvam AI** | Translates the alert into the owner's language (22 supported), speaks it (text-to-speech) and understands the spoken "haan / नहीं" reply (speech-to-text). |
| **Flagsmith** | `sale_mode` is the remediation: one flag flip makes checkout lighter instantly, with no redeploy, and is reversible. `urgency_banner_variant` powers an A/B test of festive banner copy. |

## The problem

A small D2C seller has one bad hour on sale day: the site slows, checkout starts failing, and the owner doesn't speak Kubernetes. Every failed checkout is a lost festive order. SahajSale turns "something is wrong somewhere" into one sentence in Hindi, Tamil or Telugu and one safe decision.

## How it works

```
 festive traffic ──► storefront ──► cart ──► inventory   (the slow upstream)
                        │  └────────────► payments
                        │ reads flags (Flagsmith: sale_mode, urgency_banner_variant)
                        ▼
   NudgeBee AI-SRE ── investigates ──► runbook ──► POST /api/webhooks/nudgebee ─┐
   (own probes also detect and give a quick RCA if NudgeBee is not connected) ──┤
                                                                                ▼
                        SahajSale Sidekick: proposes ONE action: turn on `sale_mode`
                                 │  Sarvam translate + TTS  → spoken alert in the owner's language
                                 │  Sarvam STT              ← owner answers "haan"  (or taps a button)
                                 ▼
              Flagsmith flag flip  +  callback to the NudgeBee runbook ("approved")
                                 ▼
              live test checkouts prove recovery ─► spoken summary ─► NudgeBee AI-FinOps: "after the sale, save ₹X/month"
```

**What Sale Mode actually does** (see `shop/app.py`): it hides the recommendations widget (which calls the slow inventory service), serves stock from a cache, and reserves stock *after* payment, off the critical path. Failed deferred reservations go to a reconcile queue, which the storefront reports. Checkout stays fast even while inventory is still broken. The tests prove this.

## Run it

You need Python 3.10+.

```bash
pip install -r requirements.txt
cp .env.example .env          # optional: add sponsor keys (see below)
bash scripts/run_local.sh     # dashboard http://localhost:8000  ·  store http://localhost:8001
```

Then open the dashboard and click **Start festive traffic**, then **Break inventory**. Within seconds you get an incident with a spoken alert; type or say *haan* and watch it recover.

Other ways to run it:

- **Docker:** `docker compose up --build`
- **Kubernetes** (what NudgeBee needs to watch): see `k8s/sahajsale.yaml` (header has the exact commands) and `docs/NUDGEBEE.md`
- **Break things for real on Kubernetes:** `python3 scripts/chaos.py bad-deploy | oom | rollback | heal-k8s`
- **Test everything:** `python3 scripts/e2e_test.py` runs the full story in mock mode (about 1 minute)

## Sponsor keys and what mock mode means

With no keys everything still runs. The dashboard shows chips for each integration: **live** (green) or **mock** (amber). Nothing pretends to be live when it is not.

- **Sarvam** (`SARVAM_API_KEY`): live translate, speech and voice approval. In mock mode, Hindi uses built-in templates, other languages fall back to English text, the browser speaks it, and you answer by typing.
- **Flagsmith** (`FLAGSMITH_ENV_KEY` + `FLAGSMITH_ADMIN_KEY`, plus `FLAGSMITH_PROJECT_ID` or `FLAGSMITH_FEATURE_IDS`): create two features, `sale_mode` (boolean, off) and `urgency_banner_variant` (enabled, string value `A` or `B`). Keep change-request workflows **off** on that environment or the API write is refused.
- **NudgeBee:** see `docs/NUDGEBEE.md`. Until it is connected, SahajSale's own monitor detects and does a quick probe-based RCA, and the dashboard says so ("Investigated by sahajsale-monitor") instead of claiming NudgeBee did it.

## NudgeBee integration: what is real and what is simulated

- **Built:** the Sidekick exposes `POST /api/webhooks/nudgebee` (title, severity, service, root_cause, evidence, callback_url), protected by `WEBHOOK_TOKEN` (`X-Webhook-Token` or Bearer), and calls back with `approved | rejected | failed` after the human decision. This is tested end to end (`scripts/e2e_test.py`) and the endpoint was exercised by hand with curl (token required; 401 without it, incident opened with it).
- **Prepared:** a NudgeBee cloud account, the Kubernetes agent install command, and the Automation Builder plan (Event trigger, then an HTTP request step to the webhook above). Steps are in `docs/NUDGEBEE.md`.
- **Not completed:** a live NudgeBee agent connected to a cluster. The local Kubernetes cluster (Docker Desktop on a 16 GB laptop, on event-venue Wi-Fi) did not finish starting before the deadline, so no real NudgeBee alert reached SahajSale.
- **In the demo video:** the NudgeBee step is a **simulated webhook** (a curl call using NudgeBee's payload shape), labelled as such on screen and in the narration. Nothing in the video presents it as a live NudgeBee run.
- The webhook contract is ours; a NudgeBee workflow HTTP step can call it as documented, but that wiring is unverified.

## Honest limitations

- The Sarvam and Flagsmith calls were written against their public docs but could not be exercised from the build sandbox (no outbound access). Test them with real keys *before* the demo; the failure messages are explicit and appear in the incident timeline.
- Sarvam speech-to-text parameters (`saaras:v3`) follow the public quickstart. If your account uses another model, set `SARVAM_STT_MODEL`. The browser converts recordings to 16 kHz WAV to be safe.
- The NudgeBee webhook contract is ours (documented in `docs/NUDGEBEE.md`); wiring a runbook step to call it depends on NudgeBee's builder, which should be verified in your install.
- Voice approval only ever approves the single proposed action on the open incident. Unclear replies change nothing.
- `DEMO_CONTROLS=1` exposes break/heal endpoints. Set it to `0` for anything beyond a demo. Set `WEBHOOK_TOKEN` if the webhook is reachable from outside.
- FinOps numbers are demo data until NudgeBee posts real findings to `/api/webhooks/finops` (the dashboard labels the source).

## Layout

```
shop/app.py            demo storefront + cart + inventory + payments (one codebase, ROLE env), metrics, fault injection
sidekick/app.py        incident lifecycle, webhooks, approval, verification, FinOps, demo controls
sidekick/providers.py  Sarvam (translate, TTS, STT, intent parsing) and Flagsmith (read, flip) adapters
sidekick/static/       the dashboard (single page, mic recording in the browser)
k8s/                   manifests        scripts/   run_local, chaos, loadgen, e2e_test
docs/                  NUDGEBEE.md (integration + runbook)   DEMO.md (3-minute script + submission checklist)
```
