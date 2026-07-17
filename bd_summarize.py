#!/usr/bin/env python3
"""
Ignitec BD Sourcing  ->  AI Opportunity Summaries
================================================================
Reads the crawler's cumulative leads and produces a short, plain-language
summary + suggested outreach angle for each capability-aligned award and
expiring contract, so the team can scan the pipeline fast.

Design and guardrails:
- Model: claude-haiku-4-5 (cheapest capable for short summarize/classify).
- Cost control: Message Batches API (50% off) + prompt caching of the shared
  Ignitec profile (cache_control on the system block).
- Truth-first: the model summarizes ONLY the notice text we pass in. The system
  prompt forbids inventing facts, metrics, contacts, or past performance; unknown
  values are stated as unknown. Summaries never assert set-asides/clearances.
- Output is written to output/summaries.json keyed by lead id (a SEPARATE file,
  so the lead schema stays pure). The report and dashboard merge it in.
- Incremental: leads already summarized (by id) are skipped, so each run only
  pays for new leads. A per-run cap bounds spend.

Requires: pip install anthropic ; env ANTHROPIC_API_KEY (GitHub Actions secret).
Run: python bd_summarize.py
"""
import os
import json
import datetime as dt

OUTPUT_DIR = "output"
SUMMARIES_FILE = os.path.join(OUTPUT_DIR, "summaries.json")
MODEL = "claude-haiku-4-5"
TODAY = dt.date.today().isoformat()

# Load config for profile context + cap.
try:
    with open("config/ignitec.json") as f:
        _cfg = json.load(f) or {}
except (FileNotFoundError, json.JSONDecodeError, OSError):
    _cfg = {}
PROFILE = _cfg.get("profile", {})
CERTS = (_cfg.get("certifications", {}) or {})
EXPERIENCE = PROFILE.get("experience", {})
MAX_PER_RUN = int((_cfg.get("crawl", {}) or {}).get("max_summaries_per_run", 1000))
# Hard cap on how long to wait for the Anthropic batch to finish. The batches API can
# intermittently hang for an hour; without a bound the summaries step blocks the commit
# of the crawl/enrich results indefinitely. On timeout we keep existing summaries and
# let report + commit proceed; the new summaries backfill on a later run.
SUMMARY_MAX_WAIT = int((_cfg.get("crawl", {}) or {}).get("summary_max_wait_secs", 1500))

LANE_LINES = "\n".join(
    f"- {l.get('id')} (Tier {l.get('tier')}): {l.get('title')}" for l in PROFILE.get("lanes", [])
)

# Grounding facts for the fit assessment, straight from the profile (no invention).
_held = ", ".join(CERTS.get("held", [])) or "small business"
_pending = ", ".join(CERTS.get("pending", [])) or "none"
_dontclaim = ", ".join(CERTS.get("do_not_claim", []))
_proven_ag = ", ".join(EXPERIENCE.get("proven_agencies", []))
_primes = ", ".join(EXPERIENCE.get("prime_relationships", []))

SYSTEM = f"""You summarize U.S. federal contracting opportunities for Ignitec Inc, a small
disadvantaged business (SDB, minority-owned) that provides IT staffing and services.
Ignitec pursues two plays: (1) subcontracting/staffing to primes who just won awards,
and (2) getting ahead of expiring contracts to reach the contracting office early or to
sub to the incumbent.

Ignitec capability lanes:
{LANE_LINES}

Ignitec profile facts (use ONLY these for fit; do not add credentials it does not have):
- Certifications held: {_held}.
- Pending (NOT yet certified, never claim as active): {_pending}.
- Must never be claimed: {_dontclaim}.
- Agencies with real past performance (prime or subcontractor): {_proven_ag}.
- Primes Ignitec has actually subcontracted to: {_primes}.

For each opportunity you are given, return:
- summary: 2 to 3 plain sentences on what the work is and who it is for. Restate the
  agency mission in its own terms first.
- outreach_angle: one sentence on the specific angle Ignitec should use (which lane,
  sub vs. prime, warm channel vs. cold), grounded only in the data provided.
- fit: 1 to 2 sentences on why this fits Ignitec, tied to a specific lane and to any
  real past performance (a proven agency above, or a prime Ignitec already subs to).
  If the fit is weak, say so plainly.
- concerns: one short sentence naming the main risk or gap, or "" if none is evident.
  Examples: a set-aside Ignitec cannot prime (SDVOSB, WOSB, HUBZone, 8(a) as prime
  since 8(a) is only pending), a clearance or domain outside the lanes, or a very large
  ceiling where only a subcontract role is realistic.
- lanes: the Ignitec lane id(s) this maps to (e.g. "1A"), or [] if none clearly apply.

Rules: Use ONLY the information provided and the profile facts above. Never invent facts,
metrics, dollar values, past performance, clause citations, contacts, set-asides, or
clearances. Never claim 8(a), SDVOSB, WOSB, HUBZone, or DCAA status. If the description
is thin, say what is unknown rather than guessing. No superlatives. No em dashes. Use
"and" not "&". Keep it factual and short."""


def load_leads():
    for name in ("all_leads.json", "latest_new_leads.json"):
        path = os.path.join(OUTPUT_DIR, name)
        try:
            with open(path) as fh:
                data = json.load(fh)
            if isinstance(data, list) and data:
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return []


def load_summaries():
    try:
        with open(SUMMARIES_FILE) as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# Reuse the deterministic scorer's signals to ground the AI's fit rationale, so the
# two agree. Optional: if bd_report cannot be imported, we simply omit the signals.
try:
    from bd_report import score_lead as _score_lead
except Exception:  # pragma: no cover
    _score_lead = None


def _priority_signals(l):
    if not _score_lead:
        return None
    try:
        s, t, why = _score_lead(l)
    except Exception:
        return None
    return f"Tier {t} (score {s}/100): " + (", ".join(why) if why else "no strong signals")


def lead_prompt(l):
    fields = [
        ("Play", "Recent award (subcontracting target)" if l.get("source") == "Recent Award"
                  else "Expiring contract (recompete / incumbent outreach)"),
        ("Title", l.get("title")),
        ("Agency", l.get("agency")),
        ("Sub-agency", l.get("subAgency")),
        ("Prime / winner" if l.get("source") == "Recent Award" else "Incumbent",
         l.get("prime") or l.get("incumbent")),
        ("Award value", l.get("value")),
        ("NAICS", l.get("naics")),
        ("PSC", l.get("psc")),
        ("Set-aside", l.get("setAside")),
        ("Vehicle", l.get("vehicle")),
        ("Period end", l.get("popEnd")),
        ("Our scorer's read", _priority_signals(l)),
        ("Description", (l.get("notes") or "")[:1200]),
    ]
    return "\n".join(f"{k}: {v}" for k, v in fields if v not in (None, "", []))


SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "outreach_angle": {"type": "string"},
        "fit": {"type": "string"},
        "concerns": {"type": "string"},
        "lanes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "outreach_angle", "fit", "concerns", "lanes"],
    "additionalProperties": False,
}


def build_requests(leads, done):
    """One batch request per not-yet-summarized aligned award/expiring lead."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request
    reqs = []
    for l in leads:
        if l.get("source") not in ("Recent Award", "Expiring Contract"):
            continue
        prev = done.get(l.get("id"))
        # Skip only if already summarized WITH the fit/concerns fields; records from
        # before those fields existed are re-summarized once to backfill them.
        if prev is not None and "fit" in prev:
            continue
        reqs.append(Request(
            custom_id=l["id"],
            params=MessageCreateParamsNonStreaming(
                model=MODEL,
                max_tokens=550,
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": lead_prompt(l)}],
            ),
        ))
        if len(reqs) >= MAX_PER_RUN:
            print(f"  Reached per-run cap of {MAX_PER_RUN}; remaining leads summarize next run.")
            break
    return reqs


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  ! ANTHROPIC_API_KEY not set; skipping AI summaries.")
        return
    try:
        import anthropic
    except ImportError:
        print("  ! anthropic package not installed; skipping AI summaries.")
        return

    leads = load_leads()
    summaries = load_summaries()
    reqs = build_requests(leads, summaries)
    if not reqs:
        print("  AI summaries: nothing new to summarize.")
        return

    client = anthropic.Anthropic()
    print(f"  AI summaries: submitting batch of {len(reqs)} leads to {MODEL}...")
    batch = client.messages.batches.create(requests=reqs)

    import time
    deadline = time.time() + SUMMARY_MAX_WAIT
    timed_out = False
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        if time.time() > deadline:
            timed_out = True
            print(f"  AI summaries: batch {batch.id} did not finish within {SUMMARY_MAX_WAIT}s "
                  f"(status {b.processing_status}). Leaving existing summaries in place so the "
                  f"crawl and enrichment results still commit; new summaries backfill next run.")
            break
        time.sleep(20)

    added = 0
    results = [] if timed_out else client.messages.batches.results(batch.id)
    for result in results:
        if result.result.type != "succeeded":
            continue
        msg = result.result.message
        text = next((blk.text for blk in msg.content if blk.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        summaries[result.custom_id] = {
            "summary": data.get("summary", ""),
            "outreach_angle": data.get("outreach_angle", ""),
            "fit": data.get("fit", ""),
            "concerns": data.get("concerns", ""),
            "lanes": data.get("lanes", []),
            "model": MODEL,
            "dateSummarized": TODAY,
        }
        added += 1

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(SUMMARIES_FILE, "w") as fh:
        json.dump(summaries, fh, indent=2)
    print(f"  AI summaries: wrote {added} new (total {len(summaries)}) to {SUMMARIES_FILE}")


if __name__ == "__main__":
    main()
