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

OUTPUT_DIR = "output"
STATE_FILE = "state.json"

# --------------------------------------------------------- INTERNALS ---------
USA_API = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
SAM_API = "https://api.sam.gov/opportunities/v2/search"
CONTRACT_TYPES = ["A", "B", "C", "D"]
IDV_TYPES = ["IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E"]
USA_FIELDS = ["Award ID", "Recipient Name", "Awarding Agency", "Awarding Sub Agency",
              "Award Amount", "Start Date", "End Date", "Description",
              "Contract Award Type", "NAICS", "PSC"]
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
        "gates": [None, None, None, None, None, None],
        "status": "Tracking", "owner": "", "nextAction": "", "nextActionDate": "",
        "notes": "", "dateAdded": TODAY, "lastTouched": TODAY,
    }
    base.update(fields)
    return base, f"{source}|{key}"


def is_warm(name):
    n = (name or "").lower()
    return any(p in n for p in WARM_PARTNERS)


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
    out = []
    for a in raw:
        amt = a.get("Award Amount") or 0
        if amt < MIN_AWARD_VALUE:
            continue
        name = a.get("Recipient Name")
        out.append(lead(
            "Recent Award", a.get("Award ID"),
            title=f"{name} award at {a.get('Awarding Agency') or 'agency'}",
            prime=name, agency=a.get("Awarding Agency"), subAgency=a.get("Awarding Sub Agency"),
            value=amt, awardId=a.get("Award ID"), naics=a.get("NAICS"), psc=a.get("PSC"),
            popStart=a.get("Start Date"), popEnd=a.get("End Date"),
            posture="Warm - route via channel" if is_warm(name) else "Cold outreach",
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
        name = a.get("Recipient Name")
        out.append(lead(
            "Expiring Contract", a.get("Award ID"),
            title=f"Recompete: {name} at {a.get('Awarding Agency') or 'agency'} (ends {str(end_raw)[:10]})",
            prime=name, agency=a.get("Awarding Agency"), subAgency=a.get("Awarding Sub Agency"),
            value=a.get("Award Amount") or 0, awardId=a.get("Award ID"),
            naics=a.get("NAICS"), psc=a.get("PSC"),
            popStart=a.get("Start Date"), popEnd=str(end_raw)[:10], posture=posture,
            nextAction="Confirm shaping window and request current contract via FOIA",
            notes=(a.get("Description") or "")[:400],
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
    save_state(seen)
    print(f"Done. {len(new_leads)} new leads (of {len(collected)} pulled). "
          f"Import output/latest_new_leads.json into the cockpit.")


if __name__ == "__main__":
    main()
