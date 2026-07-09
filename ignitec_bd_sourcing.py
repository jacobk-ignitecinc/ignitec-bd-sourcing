#!/usr/bin/env python3
"""
Ignitec BD Sourcing Crawler  v0.2
================================================================
Runs on a schedule (GitHub Actions) and pulls potential opportunities from:

  1. USASpending.gov  -> RECENT AWARDS      (subcontracting targets)
  2. USASpending.gov  -> EXPIRING CONTRACTS (recompete shaping targets)
  3. SAM.gov          -> SOURCES SOUGHT + SPECIAL NOTICES (RFI cadence feed)

It writes a JSON array of leads that pastes straight into the BD Sourcing
Cockpit's Import button with zero reformatting, and it keeps a ledger
(state.json) so each run emits ONLY opportunities not seen before. That means
you can import every week without creating duplicates.

This crawls and collects. It does not score go/no-go, price, or make claims.

Requirements : Python 3.9+ ,  pip install requests
Secrets      : SAM_API_KEY  (free from sam.gov Account Details -> API Key)
Run          : python ignitec_bd_sourcing.py

TRUTH-FIRST NOTE: written against the documented USASpending and SAM.gov APIs
but NOT tested against the live endpoints from the authoring environment.
Treat as v0.2. On the first run, confirm the SAM parameter names and notice-type
codes against https://open.gsa.gov/api/get-opportunities-public-api/ and the
USASpending fields against https://api.usaspending.gov/docs/ , then tune CONFIG.
"""

import os
import time
import json
import hashlib
import datetime as dt
import requests

# ============================================================ CONFIG =========
# Defaults below are overridden by config/ignitec.json ("crawl" block) when that
# file is present, so targeting can be tuned without editing code.
NAICS_CODES = ["541512", "541511", "541513", "541611", "541618", "561320"]

TARGET_AGENCIES = [
    "Department of Defense",
    "Department of Health and Human Services",
    "Department of Justice",
    "General Services Administration",
]

# Motion B: recent awards
RECENT_AWARDS_LOOKBACK_DAYS = 30
MIN_AWARD_VALUE = 1_000_000

# Motion A: expiring contracts
RUN_EXPIRING_PASS = True
EXPIRING_MIN_DAYS = 60
EXPIRING_MAX_DAYS = 730
EXPIRING_LOOKBACK_YEARS = 6

# SAM.gov Sources Sought / Special Notices (RFI cadence feed)
RUN_SAM_PASS = True
SAM_LOOKBACK_DAYS = 14
SAM_PTYPES = ["r", "s"]   # r = Sources Sought, s = Special Notice (RFIs appear under both)

WARM_PARTNERS = ["deloitte", "accenture", "amyx", "icf", "optum"]

# Vehicles Ignitec holds (a match is a strong sub angle) and recipient names to
# exclude (self-awards). Overridden from config.
HELD_VEHICLE_PIIDS = ["47QTCA24D0060", "N0017825D7445"]
EXCLUDE_RECIPIENT_NAMES = ["ignitec"]

CONFIG_FILE = "config/ignitec.json"


def _apply_config():
    """Override the defaults above from config/ignitec.json if present."""
    global NAICS_CODES, TARGET_AGENCIES, WARM_PARTNERS
    global RECENT_AWARDS_LOOKBACK_DAYS, MIN_AWARD_VALUE
    global EXPIRING_MIN_DAYS, EXPIRING_MAX_DAYS, SAM_LOOKBACK_DAYS
    global RUN_EXPIRING_PASS, RUN_SAM_PASS
    global HELD_VEHICLE_PIIDS, EXCLUDE_RECIPIENT_NAMES
    try:
        with open(CONFIG_FILE) as f:
            crawl = (json.load(f) or {}).get("crawl", {})
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    NAICS_CODES = crawl.get("naics_codes") or NAICS_CODES
    TARGET_AGENCIES = crawl.get("target_agencies") or TARGET_AGENCIES
    WARM_PARTNERS = crawl.get("warm_partners") or WARM_PARTNERS
    RECENT_AWARDS_LOOKBACK_DAYS = crawl.get("recent_awards_lookback_days", RECENT_AWARDS_LOOKBACK_DAYS)
    MIN_AWARD_VALUE = crawl.get("min_award_value", MIN_AWARD_VALUE)
    EXPIRING_MIN_DAYS = crawl.get("expiring_min_days", EXPIRING_MIN_DAYS)
    EXPIRING_MAX_DAYS = crawl.get("expiring_max_days", EXPIRING_MAX_DAYS)
    SAM_LOOKBACK_DAYS = crawl.get("sam_lookback_days", SAM_LOOKBACK_DAYS)
    RUN_EXPIRING_PASS = crawl.get("run_expiring_pass", RUN_EXPIRING_PASS)
    RUN_SAM_PASS = crawl.get("run_sam_pass", RUN_SAM_PASS)
    HELD_VEHICLE_PIIDS = crawl.get("held_vehicle_piids") or HELD_VEHICLE_PIIDS
    EXCLUDE_RECIPIENT_NAMES = crawl.get("exclude_recipient_names") or EXCLUDE_RECIPIENT_NAMES
    print(f"  Config loaded: {len(NAICS_CODES)} NAICS, {len(TARGET_AGENCIES)} agencies, "
          f"{len(WARM_PARTNERS)} warm partners.")


_apply_config()

OUTPUT_DIR = "output"
STATE_FILE = "state.json"

# --------------------------------------------------------- INTERNALS ---------
USA_API = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
SAM_API = "https://api.sam.gov/opportunities/v2/search"
CONTRACT_TYPES = ["A", "B", "C", "D"]
IDV_TYPES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]
USA_FIELDS = ["Award ID", "Recipient Name", "Recipient UEI", "Awarding Agency", "Awarding Sub Agency",
              "Award Amount", "Start Date", "End Date", "Last Modified Date", "Description",
              "Contract Award Type", "NAICS", "PSC", "Type of Set Aside"]
NAICS_SET = set(NAICS_CODES)
TODAY = dt.date.today().isoformat()
SRC_PREFIX = {"Recent Award": "RA", "Expiring Contract": "EX", "RFI/Sources Sought": "RFI"}


def make_id(source, key):
    h = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:10]
    return f"{SRC_PREFIX.get(source, 'X')}-{h}"


def lead(source, key, **fields):
    """Build a cockpit-schema lead object. Returns (lead_dict, dedupe_key)."""
    base = {
        "id": make_id(source, key), "source": source,
        "title": "", "prime": "", "agency": "", "subAgency": "",
        "value": "", "awardId": "", "solicitationNumber": "", "naics": "", "psc": "",
        "popStart": "", "popEnd": "", "posture": "", "tier": "", "lane": "",
        "setAside": "", "vehicle": "", "vehicleHeld": False, "incumbent": "", "routing": "", "url": "",
        "actionDate": "", "awardeeUei": "",
        "gates": [None, None, None, None, None, None],
        "status": "Tracking", "owner": "", "nextAction": "", "nextActionDate": "",
        "notes": "", "dateAdded": TODAY, "lastTouched": TODAY,
    }
    base.update(fields)
    return base, f"{source}|{key}"


def is_warm(name):
    n = (name or "").lower()
    return any(p in n for p in WARM_PARTNERS)


def routing_for(name):
    """Warm partners get routed through the existing channel; everyone else is cold."""
    return "Warm - route via channel" if is_warm(name) else "Cold outreach"


def is_self(name):
    n = (name or "").lower()
    return any(x in n for x in EXCLUDE_RECIPIENT_NAMES)


def parse_vehicle(generated_internal_id):
    """The parent IDV (vehicle) PIID is embedded in USASpending's generated_internal_id,
    e.g. CONT_AWD_<piid>_<subtier>_<parentPIID>_<parentSubtier>. Returns (piid, held)."""
    gid = str(generated_internal_id or "")
    parts = gid.split("_")
    piid = ""
    if gid.startswith("CONT_AWD_") and len(parts) >= 5:
        cand = parts[4]
        if cand and cand not in ("-NONE-", "NONE"):
            piid = cand
    return piid, (piid in HELD_VEHICLE_PIIDS)


def usa_award_url(generated_internal_id):
    """The public USASpending award page for a lead, from its generated_internal_id.
    e.g. https://www.usaspending.gov/award/CONT_AWD_.../ . Empty if the id is missing."""
    gid = str(generated_internal_id or "").strip()
    return f"https://www.usaspending.gov/award/{gid}/" if gid else ""


def code_of(v):
    """USASpending returns NAICS/PSC as {"code": ..., "description": ...} objects.
    The cockpit schema wants the bare code string. Flatten, tolerating older
    string-shaped responses."""
    if isinstance(v, dict):
        return v.get("code") or ""
    return v or ""


# Expected result keys per feed; a live response missing one is logged loudly
# rather than silently dropped (see project guide: "do not silently drop fields").
USA_EXPECTED = {"Award ID", "Recipient Name", "Awarding Agency", "Award Amount",
                "Start Date", "End Date", "NAICS", "PSC"}
SAM_EXPECTED = {"noticeId", "title", "fullParentPathName", "naicsCode", "responseDeadLine"}


def warn_missing(sample, expected, feed):
    missing = [k for k in expected if k not in sample]
    if missing:
        print(f"  ! {feed}: response missing expected field(s): {sorted(missing)}. "
              f"Got keys: {sorted(sample.keys())}")


# ------------------------------------------------------- USASpending ---------
def usa_query(award_type_codes, start_date, end_date):
    rows, page = [], 1
    while True:
        payload = {
            "filters": {
                "award_type_codes": award_type_codes,
                "time_period": [{"start_date": start_date, "end_date": end_date, "date_type": "action_date"}],
                "naics_codes": NAICS_CODES,
                "agencies": [{"type": "awarding", "tier": "toptier", "name": n} for n in TARGET_AGENCIES],
            },
            "fields": USA_FIELDS, "page": page, "limit": 100,
            "sort": "Award Amount", "order": "desc",
        }
        try:
            r = requests.post(USA_API, json=payload, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  ! USASpending request failed (page {page}): {e}")
            break
        data = r.json()
        rows.extend(data.get("results", []))
        if not data.get("page_metadata", {}).get("hasNext") or page >= 50:
            break
        page += 1
        time.sleep(0.3)
    return rows


def recent_awards():
    end = dt.date.today()
    start = end - dt.timedelta(days=RECENT_AWARDS_LOOKBACK_DAYS)
    raw = (usa_query(CONTRACT_TYPES, start.isoformat(), end.isoformat())
           + usa_query(IDV_TYPES, start.isoformat(), end.isoformat()))
    if raw:
        warn_missing(raw[0], USA_EXPECTED, "Recent Award")
    out = []
    for a in raw:
        amt = a.get("Award Amount") or 0
        if amt < MIN_AWARD_VALUE:
            continue
        name = a.get("Recipient Name")
        if is_self(name):
            continue   # exclude Ignitec's own awards
        vehicle, held = parse_vehicle(a.get("generated_internal_id"))
        out.append(lead(
            "Recent Award", a.get("Award ID"),
            title=f"{name} award at {a.get('Awarding Agency') or 'agency'}",
            prime=name, agency=a.get("Awarding Agency"), subAgency=a.get("Awarding Sub Agency"),
            value=amt, awardId=a.get("Award ID"), naics=code_of(a.get("NAICS")), psc=code_of(a.get("PSC")),
            popStart=a.get("Start Date"), popEnd=a.get("End Date"), url=usa_award_url(a.get("generated_internal_id")),
            actionDate=str(a.get("Last Modified Date") or "")[:10], awardeeUei=a.get("Recipient UEI") or "",
            setAside=a.get("Type of Set Aside") or "", vehicle=vehicle, vehicleHeld=held,
            posture=routing_for(name), routing=routing_for(name),
            nextAction="Cold outreach to prime for subcontracting/staffing"
                       if not is_warm(name) else "Route to existing channel; offer to sub",
            notes=(a.get("Description") or "")[:400],
        ))
    return out


def expiring_contracts():
    today = dt.date.today()
    lo = today + dt.timedelta(days=EXPIRING_MIN_DAYS)
    hi = today + dt.timedelta(days=EXPIRING_MAX_DAYS)
    scan_start = today - dt.timedelta(days=365 * EXPIRING_LOOKBACK_YEARS)
    raw = (usa_query(CONTRACT_TYPES, scan_start.isoformat(), today.isoformat())
           + usa_query(IDV_TYPES, scan_start.isoformat(), today.isoformat()))
    if raw:
        warn_missing(raw[0], USA_EXPECTED, "Expiring Contract")
    out = []
    for a in raw:
        end_raw = a.get("End Date")
        if not end_raw:
            continue
        try:
            end = dt.date.fromisoformat(str(end_raw)[:10])
        except ValueError:
            continue
        if not (lo <= end <= hi):
            continue
        days = (end - today).days
        posture = "Shape" if days >= 540 else "Position" if days >= 180 else "Compete/Team"
        name = a.get("Recipient Name")   # the incumbent on the expiring contract
        if is_self(name):
            continue
        vehicle, held = parse_vehicle(a.get("generated_internal_id"))
        # 9-18 months out is the recompete-shaping sweet spot for CO/COR outreach.
        shaping = 270 <= days <= 540
        out.append(lead(
            "Expiring Contract", a.get("Award ID"),
            title=f"Recompete: {name} at {a.get('Awarding Agency') or 'agency'} (ends {str(end_raw)[:10]})",
            prime=name, agency=a.get("Awarding Agency"), subAgency=a.get("Awarding Sub Agency"),
            value=a.get("Award Amount") or 0, awardId=a.get("Award ID"),
            naics=code_of(a.get("NAICS")), psc=code_of(a.get("PSC")),
            popStart=a.get("Start Date"), popEnd=str(end_raw)[:10], posture=posture,
            url=usa_award_url(a.get("generated_internal_id")),
            actionDate=str(a.get("Last Modified Date") or "")[:10], awardeeUei=a.get("Recipient UEI") or "",
            setAside=a.get("Type of Set Aside") or "", vehicle=vehicle, vehicleHeld=held,
            incumbent=name, routing=routing_for(name),
            nextAction=("Reach CO/COR now to shape the recompete" if shaping
                        else "Track; engage incumbent about subcontracting"),
            notes=(("SHAPING WINDOW. " if shaping else "") + (a.get("Description") or ""))[:400],
        ))
    return out


# ---------------------------------------------------------- SAM.gov ----------
def sam_opportunities():
    key = os.environ.get("SAM_API_KEY")
    if not key:
        print("  ! SAM_API_KEY not set; skipping SAM pull.")
        return []
    posted_from = (dt.date.today() - dt.timedelta(days=SAM_LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    posted_to = dt.date.today().strftime("%m/%d/%Y")
    raw = []
    for pt in SAM_PTYPES:
        offset = 0
        while True:
            params = {"api_key": key, "postedFrom": posted_from, "postedTo": posted_to,
                      "ptype": pt, "limit": 100, "offset": offset}
            try:
                r = requests.get(SAM_API, params=params, timeout=60)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"  ! SAM request failed (ptype {pt}): {e}")
                break
            data = r.json()
            batch = data.get("opportunitiesData", [])
            raw.extend(batch)
            total = data.get("totalRecords", 0)
            offset += 100
            if offset >= total or not batch:
                break
            time.sleep(0.5)
    if raw:
        warn_missing(raw[0], SAM_EXPECTED, "RFI/Sources Sought")
    out = []
    for o in raw:
        naics = o.get("naicsCode") or ""
        if naics and naics not in NAICS_SET:   # keep in-lane; blank-NAICS notices pass through
            continue
        deadline = (o.get("responseDeadLine") or "")[:10]
        okey = o.get("noticeId") or o.get("solicitationNumber")
        out.append(lead(
            "RFI/Sources Sought", okey,
            title=o.get("title"),
            agency=o.get("fullParentPathName"),
            solicitationNumber=o.get("solicitationNumber") or o.get("noticeId"),
            naics=naics, psc=o.get("classificationCode"),
            nextAction="Draft RFI / Sources Sought response",
            nextActionDate=deadline,
            notes=f"{o.get('type', 'Notice')}. Response due {deadline or 'see notice'}. "
                  f"Set tier to trigger RFI override. {o.get('uiLink', '')}",
        ))
    return out


# ------------------------------------------------------------- run -----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("seen", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(seen):
    with open(STATE_FILE, "w") as f:
        json.dump({"seen": sorted(seen), "updated": TODAY}, f, indent=2)


def main():
    seen = load_state()
    collected = []
    print("Recent awards...");  collected += recent_awards()
    if RUN_EXPIRING_PASS:
        print("Expiring contracts..."); collected += expiring_contracts()
    if RUN_SAM_PASS:
        print("SAM sources sought / special notices..."); collected += sam_opportunities()

    new_leads = []
    for obj, dedupe_key in collected:
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        new_leads.append(obj)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = TODAY.replace("-", "")
    with open(os.path.join(OUTPUT_DIR, f"new_leads_{stamp}.json"), "w") as f:
        json.dump(new_leads, f, indent=2)
    with open(os.path.join(OUTPUT_DIR, "latest_new_leads.json"), "w") as f:
        json.dump(new_leads, f, indent=2)

    # Cumulative standing set: merge EVERY lead pulled this run into
    # output/all_leads.json (dedup by id), not just the newly-unseen ones. The
    # dedupe ledger keeps latest_new_leads.json to "new only" for the import
    # cadence, but the report, dashboard, and expiring watch need the full
    # current picture (all in-window awards, expiring contracts, and RFIs),
    # which would otherwise be filtered out as already-seen. Earlier entries are
    # kept so history accumulates.
    all_path = os.path.join(OUTPUT_DIR, "all_leads.json")
    try:
        with open(all_path) as f:
            master = {l["id"]: l for l in json.load(f) if isinstance(l, dict) and l.get("id")}
    except (FileNotFoundError, json.JSONDecodeError):
        master = {}
    # all_leads.json holds only crawler output (triage lives in the dashboard), so it
    # is safe to refresh existing records with the latest crawl data. This backfills
    # newly added fields onto previously-seen leads. First-seen dateAdded is preserved.
    added = 0
    for obj, _dedupe_key in collected:
        prev = master.get(obj["id"])
        if prev is None:
            added += 1
        else:
            obj["dateAdded"] = prev.get("dateAdded") or obj.get("dateAdded")
        master[obj["id"]] = obj
    with open(all_path, "w") as f:
        json.dump(sorted(master.values(), key=lambda l: l.get("dateAdded", ""), reverse=True), f, indent=2)

    save_state(seen)
    print(f"Done. {len(new_leads)} new leads (of {len(collected)} pulled). "
          f"Cumulative all_leads.json now holds {len(master)} leads (+{added}). "
          f"Import output/latest_new_leads.json into the cockpit.")


if __name__ == "__main__":
    main()
