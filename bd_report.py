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


# PSC classes that indicate IT / professional-services work (support our lanes).
IN_LANE_PSC_PREFIXES = ("D", "R", "H")


def psc_in_lane(psc):
    return bool(psc) and str(psc)[:1].upper() in IN_LANE_PSC_PREFIXES


def aligned(lead):
    """Capability prefilter: is this lead in an Ignitec lane at all? True if it maps
    to a profile lane (keyword/NAICS), sits in a core NAICS, or carries an in-lane PSC."""
    tier, _, _ = lane_match(lead)
    return tier is not None or str(lead.get("naics") or "") in NAICS_SET or psc_in_lane(lead.get("psc"))


SUMMARIES = {}


def load_summaries():
    try:
        with open(os.path.join(OUTPUT_DIR, "summaries.json")) as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def summary_text(lead):
    s = SUMMARIES.get(lead.get("id"))
    if not s:
        return ""
    out = s.get("summary", "")
    if s.get("outreach_angle"):
        out += "  Angle: " + s["outreach_angle"]
    return out


ENRICHMENT = {}


def load_enrichment():
    try:
        with open(os.path.join(OUTPUT_DIR, "enrichment.json")) as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def enrich_of(lead):
    return ENRICHMENT.get(lead.get("id")) or {}


def contact_text(lead):
    """Top contracting-office contact from HigherGov, as 'Name <email>'."""
    contacts = enrich_of(lead).get("contacts") or []
    if not contacts:
        return ""
    c = contacts[0]
    label = c.get("name") or c.get("email") or ""
    if c.get("email") and c.get("name"):
        label = f"{c['name']} <{c['email']}>"
    return label


def incumbent_text(lead):
    """Prefer the HigherGov incumbent (clean name + parent) over the raw field."""
    inc = enrich_of(lead).get("incumbent") or {}
    name = inc.get("name") or lead.get("incumbent") or lead.get("prime") or ""
    parent = inc.get("parent")
    if parent and parent.lower() != (name or "").lower():
        name = f"{name} ({parent})"
    return name


def lane_label(lead):
    tier, lid, title = lane_match(lead)
    if lid:
        return f"{lid} (T{tier})"
    if str(lead.get("naics") or "") in NAICS_SET:
        return f"NAICS {lead.get('naics')}"
    if psc_in_lane(lead.get("psc")):
        return f"PSC {lead.get('psc')}"
    return "-"


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


def routing_tag(lead):
    return "Warm" if is_warm(lead.get("prime") or lead.get("incumbent")) else "Cold"


def setaside_short(lead):
    s = (lead.get("setAside") or "").strip()
    if not s or s.upper() in ("NO SET ASIDE USED.", "NONE"):
        return "-"
    return s.title().replace("Set-Aside", "SA").replace("Set Aside", "SA")


def vehicle_disp(lead):
    # Prefer HigherGov's friendly vehicle name; fall back to the raw parent PIID.
    v = enrich_of(lead).get("vehicle") or lead.get("vehicle") or ""
    if not v:
        return "-"
    return v + (" (HELD)" if lead.get("vehicleHeld") else "")


# --------------------------------------------------------------- bucketing ---
def bucket(leads):
    """Prefilter to capability-aligned leads, then split into the two plays.
    Returns (awards, expiring, dropped) where dropped is the out-of-lane count."""
    awards, expiring, dropped = [], [], 0
    for l in leads:
        if not aligned(l):
            dropped += 1
            continue
        s = l.get("source")
        if s == "Recent Award":
            awards.append(l)
        elif s == "Expiring Contract":
            d = days_until(l.get("popEnd"))
            if d is not None and d >= 0:   # upcoming only
                expiring.append(l)
    # Awards: warm partners first (route via channel), then by value.
    awards.sort(key=lambda l: (is_warm(l.get("prime")), to_num(l.get("value"))), reverse=True)
    # Expiring: shaping-window (9-18 mo) first, then soonest end date.
    def exp_key(l):
        d = days_until(l.get("popEnd"))
        d = d if d is not None else 10**6
        shaping = 270 <= d <= 540
        return (0 if shaping else 1, d)
    expiring.sort(key=exp_key)
    return awards, expiring, dropped


# --------------------------------------------------------------- markdown ----
def md_cell(s):
    return str(s if s is not None else "").replace("|", "/").replace("\n", " ").strip()


def md_trunc(s, n=70):
    s = md_cell(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def render_markdown(awards, expiring, dropped, source_name):
    L = []
    L.append(f"# Ignitec BD Sourcing Report  ({TODAY.isoformat()})")
    L.append("")
    L.append(f"Source: `output/{source_name}`  |  Recent Awards (aligned): {len(awards)}  |  "
             f"Expiring Contracts (aligned): {len(expiring)}  |  Out-of-lane filtered out: {dropped}")
    L.append("")
    L.append("> Capability prefilter: only opportunities that map to an Ignitec lane (keyword, NAICS, "
             "or PSC) are shown. Warm = winning prime / incumbent is an existing Ignitec channel "
             "(route via relationship, do not cold-call). Contact is the contracting-office point of "
             "contact from HigherGov (blank until enrichment resolves the record); the outreach target "
             "is the prime (awards) or incumbent (expiring).")
    L.append("")

    def overflow(items):
        return f"\n_Showing top {SECTION_CAP} of {len(items)}. See output/{source_name} for the full set._\n" if len(items) > SECTION_CAP else ""

    # 1. Recent Awards -> subcontracting / staffing outreach
    L.append("## 1. Recent Awards: subcontracting and staffing outreach")
    L.append("")
    if not awards:
        L.append("_No aligned recent awards in the current set._")
    else:
        L.append("| Route | Prime (target) | Agency | Value | Lane | Set-aside | Vehicle | Contact | Summary |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for l in awards[:SECTION_CAP]:
            blurb = summary_text(l) or l.get("nextAction") or ""
            L.append(f"| {routing_tag(l)} | {md_trunc(l.get('prime'), 30)} "
                     f"| {md_trunc(l.get('agency'), 28)} | {fmt_value(l.get('value'))} "
                     f"| {lane_label(l)} | {md_trunc(setaside_short(l), 22)} | {md_cell(vehicle_disp(l))} "
                     f"| {md_trunc(contact_text(l), 46)} | {md_trunc(blurb, 80)} |")
        L.append(overflow(awards))
    L.append("")

    # 2. Expiring -> recompete shaping / incumbent outreach
    L.append("## 2. Expiring Contracts: recompete shaping and incumbent outreach")
    L.append("")
    if not expiring:
        L.append("_No aligned expiring contracts in the current set._")
    else:
        L.append("| End date | Days | Shaping | Incumbent (target) | CO / contact | Agency | Value | Lane | Route | Summary |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for l in expiring[:SECTION_CAP]:
            d = days_until(l.get("popEnd"))
            shaping = "Yes" if (d is not None and 270 <= d <= 540) else ""
            blurb = summary_text(l) or l.get("nextAction") or ""
            L.append(f"| {md_cell(l.get('popEnd')) or '-'} | {d if d is not None else '-'} | {shaping} "
                     f"| {md_trunc(incumbent_text(l), 24)} | {md_trunc(contact_text(l), 44)} "
                     f"| {md_trunc(l.get('agency'), 20)} "
                     f"| {fmt_value(l.get('value'))} | {lane_label(l)} | {routing_tag(l)} "
                     f"| {md_trunc(blurb, 70)} |")
        L.append(overflow(expiring))
    L.append("")
    L.append(f"_Generated {TODAY.isoformat()} by bd_report.py. Work these in the BD Sourcing Cockpit: "
             f"set owner, next action, and log outreach._")
    return "\n".join(L)


# --------------------------------------------------------------- html --------
def h(s):
    return html.escape(str(s if s is not None else ""))


def contact_html(lead):
    """First HigherGov contact as a name + mailto link, with phone if present."""
    contacts = enrich_of(lead).get("contacts") or []
    if not contacts:
        return "<span class='muted'>-</span>"
    c = contacts[0]
    name = h(c.get("name") or c.get("email") or "contact")
    email = c.get("email") or ""
    phone = c.get("phone") or ""
    inner = f"<a href='mailto:{h(email)}'>{name}</a>" if email else name
    if phone:
        inner += f"<br><span class='muted'>{h(phone)}</span>"
    return inner


def render_html(awards, expiring, dropped, source_name):
    def route_span(l):
        r = routing_tag(l)
        return f"<span class='tag {r.lower()}'>{r}</span>"

    def rows_awards(items):
        out = []
        for l in items[:SECTION_CAP]:
            out.append(f"<tr><td>{route_span(l)}</td><td>{h(l.get('prime'))}</td>"
                       f"<td>{h(l.get('agency'))}</td><td class='num'>{h(fmt_value(l.get('value')))}</td>"
                       f"<td>{h(lane_label(l))}</td><td>{h(setaside_short(l))}</td>"
                       f"<td>{h(vehicle_disp(l))}</td><td>{contact_html(l)}</td>"
                       f"<td class='why'>{h(summary_text(l) or l.get('nextAction') or '')}</td></tr>")
        return "".join(out)

    def rows_exp(items):
        out = []
        for l in items[:SECTION_CAP]:
            d = days_until(l.get("popEnd"))
            shaping = "<span class='tag shape'>Shaping</span>" if (d is not None and 270 <= d <= 540) else ""
            out.append(f"<tr><td>{h(l.get('popEnd') or '-')}</td><td class='num'>{d if d is not None else '-'}</td>"
                       f"<td>{shaping}</td><td>{h(incumbent_text(l))}</td>"
                       f"<td>{contact_html(l)}</td>"
                       f"<td>{h(l.get('agency'))}</td><td class='num'>{h(fmt_value(l.get('value')))}</td>"
                       f"<td>{h(lane_label(l))}</td><td>{route_span(l)}</td>"
                       f"<td class='why'>{h(summary_text(l) or l.get('nextAction') or '')}</td></tr>")
        return "".join(out)

    def overflow(items):
        return (f"<p class='ovf'>Showing top {SECTION_CAP} of {len(items)}. "
                f"Full set in output/{source_name}.</p>") if len(items) > SECTION_CAP else ""

    empty = "<p class='empty'>None aligned in the current set.</p>"
    aw_head = ("<table><tr><th>Route</th><th>Prime (target)</th><th>Agency</th><th>Value</th>"
               "<th>Lane</th><th>Set-aside</th><th>Vehicle</th><th>Contact</th><th>Summary</th></tr>")
    ex_head = ("<table><tr><th>End date</th><th>Days</th><th>Shaping</th><th>Incumbent (target)</th>"
               "<th>CO / contact</th><th>Agency</th><th>Value</th><th>Lane</th><th>Route</th><th>Summary</th></tr>")
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
 .muted{{color:#8a94a3;font-size:11.5px}} td a{{color:#1F3864}}
 tr:hover td{{background:#fafbfc}}
 .tag{{font-weight:bold;padding:1px 7px;border-radius:12px;font-size:11px}}
 .tag.warm{{background:#e6f4ec;color:#1b7a43}} .tag.cold{{background:#eef1fb;color:#1F3864}}
 .tag.shape{{background:#fbf1dc;color:#946200}}
 .ovf,.empty{{color:#5b6472;font-size:12px}} a{{color:#1F3864}}
 .foot{{color:#5b6472;font-size:12px;margin-top:24px}}
</style></head><body><div class="wrap">
<h1>Ignitec BD Sourcing Report</h1>
<div class="meta">{h(TODAY.isoformat())} &nbsp;|&nbsp; source output/{h(source_name)} &nbsp;|&nbsp;
 Recent Awards {len(awards)} &nbsp;|&nbsp; Expiring {len(expiring)} &nbsp;|&nbsp; Out-of-lane filtered {dropped}</div>
<div class="note">Capability prefilter: only opportunities that map to an Ignitec lane (keyword, NAICS,
 or PSC) are shown. <b>Warm</b> = the winning prime or incumbent is an existing Ignitec channel
 (route via the relationship, do not cold-call). Contacts (CO/COR, prime POC) populate once HigherGov
 enrichment is connected; today the outreach target is the prime (awards) or the incumbent (expiring).</div>

<h2>1. Recent Awards: subcontracting and staffing outreach</h2>
{(aw_head + rows_awards(awards) + "</table>" + overflow(awards)) if awards else empty}

<h2>2. Expiring Contracts: recompete shaping and incumbent outreach</h2>
{(ex_head + rows_exp(expiring) + "</table>" + overflow(expiring)) if expiring else empty}

<p class="foot">Generated by bd_report.py. Work these in the BD Sourcing Cockpit: set owner,
 next action, and log outreach.</p>
</div></body></html>"""


# --------------------------------------------------------------- run ---------
def main():
    global SUMMARIES, ENRICHMENT
    leads, source_name = load_leads()
    if not leads:
        print("No leads found in output/. Run the crawler first.")
        source_name = "all_leads.json"
    SUMMARIES = load_summaries()
    ENRICHMENT = load_enrichment()
    awards, expiring, dropped = bucket(leads)

    md = render_markdown(awards, expiring, dropped, source_name)
    html_doc = render_html(awards, expiring, dropped, source_name)

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

    print(f"Report written. Aligned recent awards: {len(awards)}, expiring: {len(expiring)}, "
          f"out-of-lane filtered: {dropped}. See output/report_latest.md and .html")


if __name__ == "__main__":
    main()
