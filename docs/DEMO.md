# Demo script and submission checklist

Judging is 5:00–5:45 PM; the repo, setup README and demo video are due by **5:00 PM IST**. Aim to record the video by about 3:30 PM so you have a buffer.

## The 3-minute story

**0:00 – Hook (20 s).** "It's Diwali night. Priya sells handmade diyas online. At 9 PM her checkout breaks. She doesn't speak Kubernetes, and every minute is lost orders." Show the storefront.

**0:20 – Traffic (20 s).** Dashboard → **Start festive traffic**. Orders flowing, health tiles green.

**0:40 – It breaks (30 s).** Click **Break inventory** (or run `python3 scripts/chaos.py bad-deploy` on the cluster). Tiles go red, orders fail, **revenue lost** ticks up. "Every red number is a customer who didn't get their diyas."

**1:10 – It talks (40 s).** The incident appears and the alert is spoken in Hindi. Point at three things: the root cause (inventory, with NudgeBee's evidence), the Hindi text and its English translation, and the language dropdown (switch to Tamil and let it re-speak).

**1:50 – One word (30 s).** Hold the mic and say "haan". Show the transcript on screen. The flag flips (Flagsmith), the runbook gets the callback (NudgeBee), and the timeline shows live verification.

**2:20 – Proof (25 s).** "Inventory is **still broken**, yet checkout is fast again." Show the before/after numbers, the Sale Mode chip, and the reconcile queue ("we deferred stock reservation instead of hiding the risk").

**2:45 – After the sale (15 s).** FinOps card: "NudgeBee found ₹X a month to save once the sale ends", played back in Hindi. Mention the A/B banner test on Flagsmith.

Closing line: *"Small sellers shouldn't need an SRE team on Diwali night. They need one sentence in their language, and one safe button."*

## Before you go on stage

- [ ] `python3 scripts/e2e_test.py` passes on the demo machine.
- [ ] Real Sarvam key tested: the chips on the dashboard say **live**, a Hindi alert plays, and a spoken "haan" is transcribed.
- [ ] Real Flagsmith flags created and the chip says **live**; flipping works (check change-request workflows are off). Click **Heal everything** and confirm the flag goes back OFF.
- [ ] NudgeBee installed on the cluster; the runbook fires the webhook. If it does not, say so honestly (the dashboard labels the investigator).
- [ ] Browser allowed the microphone for the dashboard origin; speakers on.
- [ ] A **recorded fallback video** of the full loop, in case Wi-Fi at the venue misbehaves.
- [ ] Heal before each run (**Heal everything**, then **Reset numbers**).

## Submission checklist (by 5:00 PM IST)

- [ ] Public GitHub repo (push this folder; make sure `.env` is not committed, `.gitignore` is included).
- [ ] README that someone else can set up from (it is in this repo; test the quick start on a clean machine if you can).
- [ ] Demo video (under 3 minutes, follow the script above).
- [ ] Mention in the submission which sponsor tool does what (table at the top of the README).
