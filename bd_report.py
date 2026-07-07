#!/usr/bin/env python3
"""
Ignitec BD Sourcing  ->  Pre-vet and Report Generator
================================================================
Reads the crawler's cumulative leads (output/all_leads.json, falling back to
output/latest_new_leads.json) and produces an action-oriented report in three
buckets:

  1. Recent Awards      -> subcontracting and outreach targets
  2. RFIs / Sources Sought -> notices to develop responses for
  3. Expiring Contracts -> upcoming period-of-performance end dates to monitor

TRUTH-FIRST DESIGN (read before changing the scoring):
- The "priority" here is a transparent TRIAGE aid for ordering the queue. It is
  NOT the six-gate go/no-go. Every priority score prints the exact signals that
  produced it, all derived from crawled data (NAICS lane match, target agency,
  award value, warm-partner match, response-deadline urgency, days-to-expiry).
- Only Gate 1 (Core Lane Alignment) is auto-SUGGESTED, and only from NAICS/PSC
  and keywords. It is advisory and labeled "confirm". Gates 2-6 (Past Performance
  Defensibility, Clearance/Compliance, Competitive Position, Effort vs PWin, Team
  Capacity) require human judgment about Ignitec and are NEVER auto-filled here.
- This script does not modify the lead JSON. It only emits report artifacts.

Outputs (in output/):
  report_latest.md, report_<YYYYMMDD>.md, report_latest.html
If GITHUB_STEP_SUMMARY is set, the Markdown is also appended there.

Run: python bd_report.py
"""
import os
import json
import html
import datetime as dt

# Reuse the crawler's config as the single source of truth for lanes/agencies.
try:
    from ignitec_bd_sourcing import NAICS_SET, TARGET_AGENCIES, WARM_PARTNERS, is_warm
except Exception:  # pragma: no cover - fallback if imported standalone
    NAICS_SET = {"541512", "541511", "541513", "541611", "541618", "561320"}
    TARGET_AGENCIES = ["Department of Defense", "Department of Health and Human Services",
                       "Department of Justice", "General Services Administration"]
    WARM_PARTNERS = ["deloitte", "accenture", "amyx", "icf", "optum"]

    def is_warm(name):
        n = (name or "").lower()
        return any(p in n for p in WARM_PARTNERS)

OUTPUT_DIR = "output"
TODAY = dt.date.today()
STAMP = TODAY.isoformat().replace("-", "")
SECTION_CAP = 30   # rows shown per bucket; overflow is reported, never silently dropped

# Load the Ignitec profile (lanes, agency tiers, warm partners). Falls back to
# built-in heuristics if config/ignitec.json is missing.
PROFILE = {}
try:
    with open("config/ignitec.json") as _f:
        PROFILE = (json.load(_f) or {}).get("profile", {})
except (FileNotFoundError, json.JSONDecodeError, OSError):
    PROFILE = {}

LANES = PROFILE.get("lanes", [])
AGENCY_TIERS = PROFILE.get("agency_tiers", {})
if PROFILE.get("warm_partners"):
    WARM_PARTNERS = PROFILE["warm_partners"]

    def is_warm(name):  # noqa: F811 - override with profile list
        n = (name or "").lower()
        return any(p in n for p in WARM_PARTNERS)

# Fallback keyword list if the profile has no lanes.
LANE_KEYWORDS = ["software", "systems", "information technology", " it ", "cyber", "data",
                 "engineering", "analytics", "cloud", "management consulting",
                 "administrative", "program management", "technical support", "modernization"]


def agency_tier(agency):
    """Return 1, 2, or None from the profile's agency_tiers (case-insensitive substring)."""
    a = (agency or "").lower()
    if any(k in a for k in AGENCY_TIERS.get("tier1", [])):
        return 1
    if any(k in a for k in AGENCY_TIERS.get("tier2", [])):
        return 2
    return None


def lane_match(lead):
    """Best (lowest-number) capability-lane tier this lead maps to, via the profile's
    lane keywords and NAICS. Returns (tier, lane_id, lane_title) or (None, None, None)."""
    hay = ((lead.get("title") or "") + " " + (lead.get("notes") or "")).lower()
    naics = str(lead.get("naics") or "")
    best = None
    for lane in LANES:
        kw_hit = any(k in hay for k in lane.get("keywords", []))
        naics_hit = naics and naics in lane.get("naics", [])
        if kw_hit or naics_hit:
            t = lane.get("tier", 3)
            if best is None or t < best[0]:
                best = (t, lane.get("id"), lane.get("title"))
    return best or (None, None, None)


# --------------------------------------------------------------- helpers -----
def load_leads():
    for name in ("all_leads.json", "latest_new_leads.json"):
        path = os.path.join(OUTPUT_DIR, name)
        try:
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data, name
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return [], None


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def days_until(datestr):
    if not datestr:
        return None
    try:
        d = dt.date.fromisoformat(str(datestr)[:10])
    except ValueError:
        return None
    return (d - TODAY).days


def fmt_value(v):
    n = to_num(v)
    if n <= 0:
        return ""
    if n >= 1e9:
        return f"${n / 1e9:.2f}B"
    if n >= 1e6:
        return f"${n / 1e6:.1f}M"
    if n >= 1e3:
        return f"${n / 1e3:.0f}K"
    return f"${n:.0f}"


_FALLBACK_AGENCY_KEYS = ["defense", "air force", "army", "navy",
                         "health and human services", "justice", "general services"]


def agency_fit(agency):
    if AGENCY_TIERS:
        return agency_tier(agency) is not None
    a = (agency or "").lower()
    return any(k in a for k in _FALLBACK_AGENCY_KEYS)


def gate1_suggestion(lead):
    """Advisory Core Lane Alignment (Gate 1) suggestion, from the Ignitec profile lanes."""
    tier, lid, _ = lane_match(lead)
    naics = str(lead.get("naics") or "")
    if tier == 1:
        return 3, f"Tier 1 lane {lid}"
    if tier == 2:
        return 2, f"Tier 2 lane {lid}"
    if naics in NAICS_SET:
        return 2, f"In-lane NAICS {naics}"
    if tier == 3:
        return 1, f"Tier 3 lane {lid}"
    return 0, "Out of core lane"


def priority(lead):
    """Transparent triage score. Returns (score, tag, reasons)."""
    src = lead.get("source")
    score, why = 0, []
    naics = str(lead.get("naics") or "")
    tier, lid, _ = lane_match(lead)
    if tier == 1:
        score += 30
        why.append(f"Tier 1 lane {lid}")
    elif tier == 2:
        score += 18
        why.append(f"Tier 2 lane {lid}")
    elif tier == 3:
        score += 6
        why.append(f"Tier 3 lane {lid}")
    elif naics in NAICS_SET:
        score += 15
        why.append(f"In-lane NAICS {naics}")

    in_lane = tier is not None or naics in NAICS_SET
    # Gate 1 is non-negotiable: with no core-lane fit, agency/urgency/value must not
    # inflate the score. Out-of-lane leads stay Low and sink in the ranking.
    if not in_lane:
        why.append("Out of core lane (Gate 1 = 0)")
        atier = agency_tier(lead.get("agency"))
        if atier:
            why.append(f"Tier {atier} agency (but out of lane)")
        return score, "Low", why

    atier = agency_tier(lead.get("agency"))
    if atier == 1:
        score += 15
        why.append("Tier 1 agency")
    elif atier == 2:
        score += 8
        why.append("Tier 2 agency")
    val = to_num(lead.get("value"))

    if src == "Recent Award":
        if is_warm(lead.get("prime")):
            score += 30
            why.append(f"Warm partner: {lead.get('prime')}")
        if val >= 50e6:
            score += 20
            why.append("Award >= $50M")
        elif val >= 10e6:
            score += 12
            why.append("Award >= $10M")
        elif val >= 1e6:
            score += 5
            why.append("Award >= $1M")

    elif src == "RFI/Sources Sought":
        if tier in (1, 2):
            why.append(f"RFI override: Tier {tier} lane responds regardless of score")
        d = days_until(lead.get("nextActionDate"))
        if d is not None and d >= 0:
            bump = 30 if d <= 7 else 20 if d <= 14 else 10 if d <= 30 else 0
            score += bump
            why.append(f"Response due in {d}d")
        elif d is not None:
            why.append("Response deadline passed")
        else:
            why.append("Response deadline not set")

    elif src == "Expiring Contract":
        d = days_until(lead.get("popEnd"))
        if d is not None:
            if d < 0:
                why.append("Already ended")
            elif d < 120:
                score += 15
                why.append(f"Ends in {d}d (engage now)")
            elif d <= 365:
                score += 20
                why.append(f"Ends in {d}d (shaping window)")
            elif d <= 730:
                score += 10
                why.append(f"Ends in {d}d (early monitor)")
        if val >= 50e6:
            score += 15
            why.append("Recompete >= $50M")
        elif val >= 10e6:
            score += 8
            why.append("Recompete >= $10M")

    # Bands calibrated to the profile-weighted range (lane up to 30, agency up to 15,
    # warm/value/urgency up to ~30). High is reserved for strong multi-signal fits.
    tag = "High" if score >= 60 else "Medium" if score >= 35 else "Low"
    return score, tag, why


def notice_link(lead):
    import re
    m = re.search(r"https?://\S+", str(lead.get("notes") or ""))
    return m.group(0) if m else ""


# --------------------------------------------------------------- bucketing ---
def bucket(leads):
    awards, rfis, expiring = [], [], []
    for l in leads:
        s = l.get("source")
        if s == "Recent Award":
            awards.append(l)
        elif s == "RFI/Sources Sought":
            d = days_until(l.get("nextActionDate"))
            if d is None or d >= 0:   # still open or undated; drop clearly past-due
                rfis.append(l)
        elif s == "Expiring Contract":
            d = days_until(l.get("popEnd"))
            if d is not None and d >= 0:   # "upcoming" only; already-ended drop off the watch
                expiring.append(l)
    awards.sort(key=lambda l: (priority(l)[0], to_num(l.get("value"))), reverse=True)
    rfis.sort(key=lambda l: (days_until(l.get("nextActionDate")) if days_until(l.get("nextActionDate")) is not None else 10**6,
                             -priority(l)[0]))
    expiring.sort(key=lambda l: (days_until(l.get("popEnd")) if days_until(l.get("popEnd")) is not None else 10**6))
    return awards, rfis, expiring


# --------------------------------------------------------------- markdown ----
def md_cell(s):
    return str(s if s is not None else "").replace("|", "/").replace("\n", " ").strip()


def md_trunc(s, n=70):
    s = md_cell(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def render_markdown(awards, rfis, expiring, source_name):
    L = []
    L.append(f"# Ignitec BD Sourcing Report  ({TODAY.isoformat()})")
    L.append("")
    L.append(f"Source: `output/{source_name}`  |  Recent Awards: {len(awards)}  |  "
             f"RFIs: {len(rfis)}  |  Expiring Contracts: {len(expiring)}")
    L.append("")
    L.append("> Priority is a transparent triage aid for ordering the queue, not the six-gate "
             "go/no-go. Gate 1 (Core Lane Alignment) is auto-suggested from NAICS and keywords "
             "and must be confirmed. Gates 2-6 require human judgment and are not auto-scored.")
    L.append("")

    def overflow(items):
        return f"\n_Showing top {SECTION_CAP} of {len(items)}. See output/{source_name} for the full set._\n" if len(items) > SECTION_CAP else ""

    # 1. Recent Awards
    L.append("## 1. Recent Awards to pursue (subcontracting and outreach)")
    L.append("")
    if not awards:
        L.append("_No recent awards in the current set._")
    else:
        L.append("| Priority | Award | Agency | Value | Prime | Lane NAICS | Gate 1 (confirm) | Why |")
        L.append("|---|---|---|---|---|---|---|---|")
        for l in awards[:SECTION_CAP]:
            sc, tag, why = priority(l)
            g1, _ = gate1_suggestion(l)
            L.append(f"| {tag} ({sc}) | {md_trunc(l.get('awardId') or l.get('title'), 40)} "
                     f"| {md_trunc(l.get('agency'), 34)} | {fmt_value(l.get('value'))} "
                     f"| {md_trunc(l.get('prime'), 30)} | {md_cell(l.get('naics'))} | {g1} "
                     f"| {md_trunc('; '.join(why), 60)} |")
        L.append(overflow(awards))
    L.append("")

    # 2. RFIs
    L.append("## 2. RFIs and Sources Sought to develop responses for")
    L.append("")
    if not rfis:
        L.append("_No open RFIs in the current set._")
    else:
        L.append("| Priority | Response due | Title | Agency | NAICS | Gate 1 | Why |")
        L.append("|---|---|---|---|---|---|---|")
        for l in rfis[:SECTION_CAP]:
            sc, tag, why = priority(l)
            g1, _ = gate1_suggestion(l)
            due = l.get("nextActionDate") or "not set"
            L.append(f"| {tag} ({sc}) | {md_cell(due)} | {md_trunc(l.get('title'), 46)} "
                     f"| {md_trunc(l.get('agency'), 30)} | {md_cell(l.get('naics')) or '-'} | {g1} "
                     f"| {md_trunc('; '.join(why), 46)} |")
        L.append(overflow(rfis))
    L.append("")

    # 3. Expiring
    L.append("## 3. Upcoming contract end dates to monitor")
    L.append("")
    if not expiring:
        L.append("_No expiring contracts in the current set._")
    else:
        L.append("| End date | Days out | Posture | Title | Agency | Value | Why |")
        L.append("|---|---|---|---|---|---|---|")
        for l in expiring[:SECTION_CAP]:
            sc, tag, why = priority(l)
            d = days_until(l.get("popEnd"))
            L.append(f"| {md_cell(l.get('popEnd')) or '-'} | {d if d is not None else '-'} "
                     f"| {md_cell(l.get('posture')) or '-'} | {md_trunc(l.get('title'), 40)} "
                     f"| {md_trunc(l.get('agency'), 28)} | {fmt_value(l.get('value'))} "
                     f"| {md_trunc('; '.join(why), 40)} |")
        L.append(overflow(expiring))
    L.append("")
    L.append(f"_Generated {TODAY.isoformat()} by bd_report.py. Work these in the BD Sourcing "
             f"Cockpit: score the six gates and set owner, posture, and next action._")
    return "\n".join(L)


# --------------------------------------------------------------- html --------
def h(s):
    return html.escape(str(s if s is not None else ""))


def render_html(awards, rfis, expiring, source_name):
    def rows_awards(items):
        out = []
        for l in items[:SECTION_CAP]:
            sc, tag, why = priority(l)
            g1, _ = gate1_suggestion(l)
            out.append(f"<tr><td><span class='tag {tag.lower()}'>{tag}</span> {sc}</td>"
                       f"<td>{h(l.get('awardId') or l.get('title'))}</td><td>{h(l.get('agency'))}</td>"
                       f"<td class='num'>{h(fmt_value(l.get('value')))}</td><td>{h(l.get('prime'))}</td>"
                       f"<td>{h(l.get('naics'))}</td><td class='num'>{g1}</td><td class='why'>{h('; '.join(why))}</td></tr>")
        return "".join(out)

    def rows_rfis(items):
        out = []
        for l in items[:SECTION_CAP]:
            sc, tag, why = priority(l)
            g1, _ = gate1_suggestion(l)
            link = notice_link(l)
            title = h(l.get('title'))
            if link:
                title = f"<a href='{h(link)}' target='_blank' rel='noopener'>{title}</a>"
            out.append(f"<tr><td><span class='tag {tag.lower()}'>{tag}</span> {sc}</td>"
                       f"<td>{h(l.get('nextActionDate') or 'not set')}</td><td>{title}</td>"
                       f"<td>{h(l.get('agency'))}</td><td>{h(l.get('naics') or '-')}</td>"
                       f"<td class='num'>{g1}</td><td class='why'>{h('; '.join(why))}</td></tr>")
        return "".join(out)

    def rows_exp(items):
        out = []
        for l in items[:SECTION_CAP]:
            sc, tag, why = priority(l)
            d = days_until(l.get("popEnd"))
            out.append(f"<tr><td>{h(l.get('popEnd') or '-')}</td><td class='num'>{d if d is not None else '-'}</td>"
                       f"<td>{h(l.get('posture') or '-')}</td><td>{h(l.get('title'))}</td>"
                       f"<td>{h(l.get('agency'))}</td><td class='num'>{h(fmt_value(l.get('value')))}</td>"
                       f"<td class='why'>{h('; '.join(why))}</td></tr>")
        return "".join(out)

    def overflow(items):
        return (f"<p class='ovf'>Showing top {SECTION_CAP} of {len(items)}. "
                f"Full set in output/{source_name}.</p>") if len(items) > SECTION_CAP else ""

    empty = "<p class='empty'>None in the current set.</p>"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ignitec BD Sourcing Report {h(TODAY.isoformat())}</title>
<link rel="icon" href="data:,">
<style>
 body{{font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;background:#f4f6f9;margin:0;font-size:13.5px}}
 .wrap{{max-width:1200px;margin:0 auto;padding:20px}}
 h1{{color:#C0222C;border-bottom:3px solid #C0222C;padding-bottom:8px;font-size:22px}}
 h2{{color:#1F3864;font-size:16px;margin-top:26px}}
 .meta{{color:#5b6472;font-size:12.5px}}
 .note{{background:#fff;border-left:4px solid #1F3864;padding:10px 14px;color:#333;border-radius:0 8px 8px 0;margin:14px 0}}
 table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dfe3ea;border-radius:8px;overflow:hidden}}
 th,td{{text-align:left;padding:7px 9px;border-bottom:1px solid #eef1f5;vertical-align:top}}
 th{{background:#1F3864;color:#fff;font-size:11.5px;text-transform:uppercase;letter-spacing:.3px}}
 td.num{{text-align:right;white-space:nowrap}} td.why{{color:#5b6472;font-size:12px}}
 tr:hover td{{background:#fafbfc}}
 .tag{{font-weight:bold;padding:1px 7px;border-radius:12px;font-size:11px}}
 .tag.high{{background:#e6f4ec;color:#1b7a43}} .tag.medium{{background:#fbf1dc;color:#946200}}
 .tag.low{{background:#eceef2;color:#5b6472}}
 .ovf,.empty{{color:#5b6472;font-size:12px}} a{{color:#1F3864}}
 .foot{{color:#5b6472;font-size:12px;margin-top:24px}}
</style></head><body><div class="wrap">
<h1>Ignitec BD Sourcing Report</h1>
<div class="meta">{h(TODAY.isoformat())} &nbsp;|&nbsp; source output/{h(source_name)} &nbsp;|&nbsp;
 Recent Awards {len(awards)} &nbsp;|&nbsp; RFIs {len(rfis)} &nbsp;|&nbsp; Expiring {len(expiring)}</div>
<div class="note">Priority is a transparent triage aid for ordering the queue, not the six-gate
 go/no-go. Gate 1 (Core Lane Alignment) is auto-suggested from NAICS and keywords and must be
 confirmed. Gates 2 through 6 require human judgment and are not auto-scored.</div>

<h2>1. Recent Awards to pursue (subcontracting and outreach)</h2>
{("<table><tr><th>Priority</th><th>Award</th><th>Agency</th><th>Value</th><th>Prime</th><th>NAICS</th><th>Gate 1</th><th>Why</th></tr>" + rows_awards(awards) + "</table>" + overflow(awards)) if awards else empty}

<h2>2. RFIs and Sources Sought to develop responses for</h2>
{("<table><tr><th>Priority</th><th>Response due</th><th>Title</th><th>Agency</th><th>NAICS</th><th>Gate 1</th><th>Why</th></tr>" + rows_rfis(rfis) + "</table>" + overflow(rfis)) if rfis else empty}

<h2>3. Upcoming contract end dates to monitor</h2>
{("<table><tr><th>End date</th><th>Days out</th><th>Posture</th><th>Title</th><th>Agency</th><th>Value</th><th>Why</th></tr>" + rows_exp(expiring) + "</table>" + overflow(expiring)) if expiring else empty}

<p class="foot">Generated by bd_report.py. Work these in the BD Sourcing Cockpit: score the six
 gates and set owner, posture, and next action.</p>
</div></body></html>"""


# --------------------------------------------------------------- run ---------
def main():
    leads, source_name = load_leads()
    if not leads:
        print("No leads found in output/. Run the crawler first.")
        source_name = "all_leads.json"
    awards, rfis, expiring = bucket(leads)

    md = render_markdown(awards, rfis, expiring, source_name)
    html_doc = render_html(awards, rfis, expiring, source_name)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "report_latest.md"), "w") as f:
        f.write(md)
    with open(os.path.join(OUTPUT_DIR, f"report_{STAMP}.md"), "w") as f:
        f.write(md)
    with open(os.path.join(OUTPUT_DIR, "report_latest.html"), "w") as f:
        f.write(html_doc)

    # Also publish a copy under docs/ so GitHub Pages (served from /docs)
    # can show the report alongside the dashboard.
    if os.path.isdir("docs"):
        with open(os.path.join("docs", "report.html"), "w") as f:
            f.write(html_doc)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a") as f:
                f.write(md + "\n")
        except OSError as e:
            print(f"  ! could not write step summary: {e}")

    print(f"Report written. Recent Awards: {len(awards)}, RFIs: {len(rfis)}, "
          f"Expiring: {len(expiring)}. See output/report_latest.md and .html")


if __name__ == "__main__":
    main()
