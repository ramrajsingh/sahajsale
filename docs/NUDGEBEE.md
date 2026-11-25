# NudgeBee integration

> **Verify before relying on this.** NudgeBee is open source (Apache-2.0) at `github.com/nudgebee/nudgebee`. This guide is based on its public descriptions, not on hands-on use. Its repo README and the hackathon's Resources tab are authoritative for install commands and runbook-builder details. Everything below that is *our* contract (webhook payloads, callbacks) is exact, because we wrote it.

## What NudgeBee does for SahajSale

- **AI-SRE** investigates the checkout incident to an evidence-cited root cause (it should trace the 504s at the storefront to the slow inventory service) and can write findings back to an incident tool.
- **Agentic Automation Builder** (no-code runbooks, triggered by chat, alert or schedule) holds our approval-gated runbook.
- **AI-FinOps** scores Kubernetes waste with a rupee/dollar figure and a fix. We show that as "after the sale, save ₹X per month".

## 1. Install it on the cluster (do this before the hackathon starts)

Per its public listing, Kubernetes is the production install path via a **Helm chart**, and the in-cluster agent is a **separate repo** (`nudgebee/k8s-agent`). Both are needed to self-host. Follow the README in `github.com/nudgebee/nudgebee`, then:

1. Deploy the demo shop: `kubectl apply -f k8s/sahajsale.yaml` (the pods expose Prometheus metrics on `:8000/metrics` and carry scrape annotations).
2. Connect NudgeBee to that cluster and give it an LLM (it supports bring-your-own-model, so use any key you have).
3. Break something and confirm AI-SRE sees it: `python3 scripts/chaos.py bad-deploy`, then `python3 scripts/chaos.py heal-k8s`.

Why a real cluster: a `bad-deploy` is a genuine rollout with failing requests and `slow request` / `timeout calling inventory` log lines, and `oom` produces a genuine `OOMKilled`. That is the evidence AI-SRE can cite. An alert-forwarding stub would not impress infrastructure judges.

## 2. The runbook (build it in the Automation Builder)

1. **Trigger:** alert on checkout failure/latency (or chat, or schedule).
2. **AI-SRE investigate** the service `storefront` in namespace `sahajsale`.
3. **Call SahajSale** with an HTTP request step to `POST http://<sahajsale-host>:8000/api/webhooks/nudgebee` using the payload below. If the builder has no HTTP step, use its webhook or notification integration instead; if neither exists, SahajSale's own monitor still detects and handles the incident, and the dashboard says so honestly.
4. **Wait for approval.** SahajSale POSTs the decision to your `callback_url` (payload below) and also exposes `GET /api/state` (incident `status`) if the step needs to poll.
5. **On approved:** continue (verify, close the incident). **On rejected/failed:** stop or escalate to a human.

### Payload NudgeBee → SahajSale (`POST /api/webhooks/nudgebee`)

```json
{
  "title": "Checkout 5xx spike",
  "severity": "critical",
  "service": "inventory",
  "root_cause": "inventory latency regression after deploy; cart times out calling inventory",
  "evidence": ["inventory p95 3.1s (baseline 40ms)", "cart: timeout calling inventory /reserve"],
  "callback_url": "http://<nudgebee-runbook-callback>"
}
```

Only `title` is required; if `service` is missing we look for `inventory`, `cart`, `payments` or `storefront` in the text. If `WEBHOOK_TOKEN` is set, send it as `X-Webhook-Token` (or `Authorization: Bearer ...`). If the Sidekick already opened an incident from its own probes, NudgeBee's RCA **replaces** the quick one and the voice alert is re-recorded.

### Callback SahajSale → NudgeBee (`POST callback_url`)

```json
{"incident_id": "a1b2c3d4", "decision": "approved", "status": "executing", "approved_by": "voice (Sarvam speech-to-text)", "at": 1764000000.0}
```

`decision` is `approved`, `rejected` or `failed`. If `NUDGEBEE_CALLBACK_TOKEN` is set it is sent as a Bearer token.

### FinOps (`POST /api/webhooks/finops`) after the sale

```json
{"findings": [{"resource": "inventory deployment", "issue": "over-provisioned after the sale", "fix": "scale 6 -> 2 replicas", "monthly_saving_inr": 9200}]}
```

The dashboard's FinOps card updates, and its Sarvam voice summary uses the new total. Until you post real findings the card shows **demo data** and says so.

## 3. Rehearse the failure scenarios

| Command | What happens | What AI-SRE should find |
|---|---|---|
| `python3 scripts/chaos.py bad-deploy` | New inventory rollout adds 3 s latency + 10% errors | Latency regression right after a deploy; cart and storefront slow only as dependents |
| `python3 scripts/chaos.py oom` | Inventory leaks memory past its 256Mi limit | `OOMKilled` restarts |
| `python3 scripts/chaos.py rollback` | `kubectl rollout undo` | Recovery after rollback |

Note the design choice: the approved fix is the **flag flip**, not a rollback. It is instant, reversible and safe for a non-technical owner. If the flag is not enough, SahajSale says so and recommends a rollback instead of pretending it worked.
