#!/usr/bin/env python3
"""
Ignitec BD Sourcing  ->  HigherGov Contact / Incumbent / Vehicle Enrichment
============================================================================
Reads the crawler's cumulative leads and, for each capability-aligned recent
award and expiring contract, looks the record up in HigherGov by its government
Award ID and backfills the three things the crawl alone cannot give us:

  1. Contracting-office contacts (name, email, phone) for the CO/COR outreach
     on expiring contracts, and the acquisition contact on recent awards.
  2. Incumbent identity (clean name, parent, UEI, CAGE) so we can also approach
     the incumbent about subcontracting.
  3. Vehicle name, so we know how the work is bought.

Design and guardrails:
- Source: HigherGov API (https://www.highergov.com/api-external/), paid tier.
  Endpoints: /contract/ and /idv/ (filtered by award_id). Both embed awardee,
  awarding_agency, vehicle, and the created_by/last_modified_by/approved_by
  people who wrote the FPDS record (the contracting-office contacts).
- Most leads are delivery orders that HigherGov does not resolve by the order
  PIID alone, so the lookup escalates: contract by award_id, then idv by
  award_id, then contract by award_id + parent, then the parent IDV (vehicle)
  record. A parent-IDV (vehicle-level) match yields the vehicle name, sponsor
  agency, and the vehicle's contracting office; it does NOT assert an order
  incumbent, since the IDV holder is not necessarily this order's winner.
  Each record carries matchLevel ("order" or "vehicle") so downstream surfaces
  can show the difference. The run prints per-tier match counts and a sample of
  misses for tuning.
- Truth-first: we copy contacts verbatim from HigherGov and label them by the
  role HigherGov actually reports (the FPDS record author/approver). We do NOT
  relabel them "CO" or "COR" unless HigherGov says so, and we never invent a
  contact. Missing values stay empty.
- Output goes to output/enrichment.json keyed by lead id (a SEPARATE file, so
  the lead schema stays pure). The report and dashboard merge it in.
- Incremental: leads already enriched (by id) are skipped, and lookups are
  cached by Award ID within a run so shared PIIDs are fetched once. A per-run
  cap bounds usage (HigherGov base plan is 10,000 records/month).

Requires: env HIGHERGOV_API_KEY (GitHub Actions secret). Uses requests.
Run: python bd_enrich.py
"""
import os
import json
import time
import datetime as dt

import requests

OUTPUT_DIR = "output"
ENRICH_FILE = os.path.join(OUTPUT_DIR, "enrichment.json")
BASE = "https://www.highergov.com/api-external"
TODAY = dt.date.today().isoformat()
TIMEOUT = 30

# Reuse the report's prefilter so we only spend lookups on aligned leads.
try:
    from bd_report import aligned
except Exception:  # pragma: no cover - standalone fallback (enrich everything)
    def aligned(_lead):
        return True

try:
    with open("config/ignitec.json") as _f:
        _cfg = json.load(_f) or {}
except (FileNotFoundError, json.JSONDecodeError, OSError):
    _cfg = {}
_CRAWL = _cfg.get("crawl", {}) or {}
RUN_ENRICHMENT = bool(_CRAWL.get("run_enrichment", True))
MAX_PER_RUN = int(_CRAWL.get("max_enrich_per_run", 1000))
RUN_SAM_POC = bool(_CRAWL.get("run_sam_poc", True))
MAX_SAM_POC_PER_RUN = int(_CRAWL.get("max_sam_poc_per_run", 1000))
SAM_ENTITY_BASE = "https://api.sam.gov/entity-information"


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


def load_enrichment():
    try:
        with open(ENRICH_FILE) as fh:
            return json.load(fh) or {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _full_url(path):
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return "https://www.highergov.com" + path


def _person(obj):
    """Normalize a HigherGov PeopleSimple object into our contact shape."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("contact_name") or " ".join(
        p for p in (obj.get("contact_first_name"), obj.get("contact_last_name")) if p
    )
    email = obj.get("contact_email") or ""
    phone = obj.get("contact_phone") or ""
    if not (name or email or phone):
        return None
    return {
        "name": (name or "").strip(),
        "email": email.strip(),
        "phone": phone.strip(),
        "title": (obj.get("contact_title") or "").strip(),
    }


def _contacts_from(record):
    """Collect the FPDS record contacts, deduped by email (then name).

    HigherGov exposes created_by / last_modified_by / approved_by on contract
    and IDV records. These are the contracting-office people who authored and
    approved the FPDS record, i.e. the acquisition point of contact. We label
    them by that provenance rather than asserting a CO/COR role we can't verify.
    """
    roles = [
        ("approved_by", "Approved by (FPDS)"),
        ("last_modified_by", "Last modified by (FPDS)"),
        ("created_by", "Created by (FPDS)"),
    ]
    out, seen = [], set()
    for field, role in roles:
        person = _person(record.get(field))
        if not person:
            continue
        key = (person["email"] or person["name"]).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        person["role"] = role
        out.append(person)
    return out


def _awardee(record):
    a = record.get("awardee") or record.get("awardee_key") or {}
    parent = record.get("awardee_parent") or record.get("awardee_key_parent") or {}
    if not isinstance(a, dict):
        a = {}
    if not isinstance(parent, dict):
        parent = {}
    name = a.get("clean_name") or ""
    if not name:
        return None
    return {
        "name": name,
        "parent": parent.get("clean_name") or "",
        "uei": a.get("uei") or "",
        "cage": a.get("cage_code") or "",
        "hgPath": _full_url(a.get("path")),
    }


def _agency_name(record):
    ag = record.get("awarding_agency") or {}
    return ag.get("agency_name") if isinstance(ag, dict) else ""


def _vehicle_name(record):
    v = record.get("vehicle") or {}
    return v.get("vehicle_name") if isinstance(v, dict) else ""


class AuthError(Exception):
    """Raised on a 401/403 so the whole run stops instead of hammering the API."""


def _query(session, api_key, path, params):
    """One HigherGov GET. Returns the first result dict, or None. Raises AuthError."""
    try:
        r = session.get(
            BASE + path,
            params={**params, "api_key": api_key, "page_size": "1"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"    ! lookup failed for {path} {params}: {e}")
        return None
    if r.status_code in (401, 403):
        raise AuthError(f"{r.status_code}: {r.text[:120]}")
    if r.status_code != 200:
        return None
    results = (r.json() or {}).get("results") or []
    return results[0] if results else None


def fetch_record(session, api_key, award_id, parent_id, parent_cache, stats):
    """Resolve a lead to a HigherGov record through a tiered lookup.

    Most of our leads are delivery orders. HigherGov often does not resolve an
    order by its order PIID alone, so we escalate:
      1. contract by award_id            (exact order)
      2. idv by award_id                 (the award itself is a vehicle)
      3. contract by award_id + parent   (order disambiguated by its parent IDV)
      4. idv by parent award_id          (the parent VEHICLE record; order-level
                                          fields unknown, so vehicle/agency only)
    Returns (record, kind, match_level) or (None, None, None) on a full miss.
    match_level is 'order' for 1-3 and 'vehicle' for 4, which the caller uses to
    decide which fields are safe to trust.
    """
    rec = _query(session, api_key, "/contract/", {"award_id": award_id})
    if rec:
        stats["order"] += 1
        return rec, "contract", "order"
    rec = _query(session, api_key, "/idv/", {"award_id": award_id})
    if rec:
        stats["order"] += 1
        return rec, "idv", "order"
    if parent_id:
        rec = _query(session, api_key, "/contract/",
                     {"award_id": award_id, "parent_award_id": parent_id})
        if rec:
            stats["order_parent"] += 1
            return rec, "contract", "order"
        # Parent IDV lookups are cached: many orders share one vehicle.
        if parent_id in parent_cache:
            rec = parent_cache[parent_id]
        else:
            rec = _query(session, api_key, "/idv/", {"award_id": parent_id})
            parent_cache[parent_id] = rec
        if rec:
            stats["vehicle"] += 1
            return rec, "idv", "vehicle"
    stats["miss"] += 1
    return None, None, None


def enrich_record(record, kind, match_level):
    contacts = _contacts_from(record)
    pop_end = (record.get("period_of_performance_current_end_date")
               or record.get("ordering_period_end_date") or "")
    data = {
        "contacts": contacts,
        "vehicle": _vehicle_name(record),
        "agency": _agency_name(record),
        "setAside": record.get("type_of_set_aside") or "",
        "popEnd": pop_end,
        "hgPath": _full_url(record.get("path")),
        "source": kind,
        "matchLevel": match_level,
        "dateEnriched": TODAY,
    }
    if match_level == "order":
        # This record IS the order/award, so its awardee and contacts are the
        # order's incumbent and contracting office.
        data["incumbent"] = _awardee(record)
    else:
        # Vehicle-level match: the IDV awardee is the vehicle holder, not
        # necessarily this order's winner, so we do NOT assert an incumbent.
        # The contacts belong to the vehicle's contracting office; relabel them.
        data["incumbent"] = None
        for c in data["contacts"]:
            c["role"] = "Vehicle " + c.get("role", "contact")
    return data


# --------------------------------------------------------------- SAM entity POC
def _poc(obj, role):
    """Normalize a SAM pointsOfContact person (public tier: name/title, no email)."""
    if not isinstance(obj, dict):
        return None
    name = " ".join(p for p in (obj.get("firstName"), obj.get("middleInitial"),
                                obj.get("lastName")) if p).strip()
    title = (obj.get("title") or "").strip()
    if not name:
        return None
    return {"name": name, "title": title, "role": role, "source": "SAM entity"}


def sam_company_pocs(session, sam_key, uei):
    """Company points of contact for a UEI from the SAM.gov Entity Management API
    (public tier -> names/titles only, no email/phone). Tries v3 then v2. Returns a
    list (possibly empty) or None on an auth/permission failure."""
    roles = [("governmentBusinessPOC", "Government Business POC"),
             ("electronicBusinessPOC", "Electronic Business POC")]
    for ver in ("v3", "v2"):
        try:
            r = session.get(
                f"{SAM_ENTITY_BASE}/{ver}/entities",
                params={"api_key": sam_key, "ueiSAM": uei,
                        "includeSections": "pointsOfContact"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"    ! SAM entity lookup failed for {uei}: {e}")
            return None
        if r.status_code in (401, 403):
            print(f"    ! SAM entity auth/permission error ({r.status_code}) for {uei}: "
                  f"{r.text[:120]}. Key may lack 'Entity: read public'.")
            return None
        if r.status_code == 404:
            continue   # try the next version
        if r.status_code != 200:
            return []
        entities = (r.json() or {}).get("entityData") or []
        if not entities:
            return []
        poc = (entities[0] or {}).get("pointsOfContact") or {}
        out = []
        for field, label in roles:
            person = _poc(poc.get(field), label)
            if person and person["name"].lower() not in {p["name"].lower() for p in out}:
                out.append(person)
        return out
    return []


def enrich_company_pocs(enrichment, sam_key):
    """Backfill each enrichment record that has an incumbent UEI with the company's
    SAM points of contact (names). Independent of the HigherGov pass, so it also
    fills records enriched on earlier runs. Cached by UEI; capped per run."""
    session = requests.Session()
    by_uei = {}
    attempted = added = 0
    for rec in enrichment.values():
        if rec.get("companyContacts") is not None:
            continue   # already resolved (even if it was an empty list)
        inc = rec.get("incumbent") or {}
        uei = (inc.get("uei") or "").strip()
        if not uei:
            continue
        if attempted >= MAX_SAM_POC_PER_RUN:
            print(f"  Reached SAM POC per-run cap of {MAX_SAM_POC_PER_RUN}.")
            break
        if uei in by_uei:
            pocs = by_uei[uei]
        else:
            attempted += 1
            pocs = sam_company_pocs(session, sam_key, uei)
            if pocs is None:      # auth failure: stop hitting SAM this run
                print("    ! Stopping SAM POC pass after auth error.")
                break
            by_uei[uei] = pocs
            time.sleep(0.3)
        rec["companyContacts"] = pocs
        added += 1
    print(f"  SAM company POCs: {attempted} UEIs looked up, wrote {added} records.")


def main():
    if not RUN_ENRICHMENT:
        print("  HigherGov enrichment disabled in config (run_enrichment=false).")
        return
    api_key = os.environ.get("HIGHERGOV_API_KEY")
    if not api_key:
        print("  ! HIGHERGOV_API_KEY not set; skipping enrichment.")
        return

    leads = load_leads()
    enrichment = load_enrichment()
    if not leads:
        print("  Enrichment: no leads found.")
        return

    session = requests.Session()
    by_award = {}       # (award_id, parent_id) -> enrichment dict, cached this run
    parent_cache = {}   # parent PIID -> parent IDV record (or None), cached this run
    stats = {"order": 0, "order_parent": 0, "vehicle": 0, "miss": 0}
    miss_samples = []
    added = 0
    attempted = 0
    try:
        for l in leads:
            if l.get("source") not in ("Recent Award", "Expiring Contract"):
                continue
            lead_id = l.get("id")
            if not lead_id or lead_id in enrichment:
                continue
            if not aligned(l):
                continue
            award_id = (l.get("awardId") or "").strip()
            if not award_id:
                continue
            # The crawler stores the parent IDV (vehicle) PIID in lead["vehicle"].
            parent_id = (l.get("vehicle") or "").strip()
            if attempted >= MAX_PER_RUN:
                print(f"  Reached per-run cap of {MAX_PER_RUN}; remaining leads enrich next run.")
                break

            cache_key = (award_id, parent_id)
            if cache_key in by_award:
                data = by_award[cache_key]
            else:
                attempted += 1
                record, kind, level = fetch_record(
                    session, api_key, award_id, parent_id, parent_cache, stats)
                data = enrich_record(record, kind, level) if record else None
                if data is None and len(miss_samples) < 20:
                    miss_samples.append({"award_id": award_id, "parent": parent_id})
                by_award[cache_key] = data
                time.sleep(0.2)   # be polite to the API

            if data:
                enrichment[lead_id] = data
                added += 1
    except AuthError as e:
        print(f"    ! HigherGov auth error ({e}); stopping. Check HIGHERGOV_API_KEY.")

    # Second pass: add company points of contact from SAM.gov (public tier, names)
    # for any enrichment record that has an incumbent UEI.
    sam_key = os.environ.get("SAM_API_KEY")
    if RUN_SAM_POC and sam_key:
        enrich_company_pocs(enrichment, sam_key)
    elif RUN_SAM_POC:
        print("  ! SAM_API_KEY not set; skipping company POC lookup.")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(ENRICH_FILE, "w") as fh:
        json.dump(enrichment, fh, indent=2)
    print(f"  HigherGov enrichment: {attempted} looked up | matched "
          f"order={stats['order']}, order+parent={stats['order_parent']}, "
          f"vehicle={stats['vehicle']}, miss={stats['miss']} | wrote {added} new "
          f"(total {len(enrichment)}) to {ENRICH_FILE}")
    if miss_samples:
        print("  Miss samples (award_id / parent PIID):")
        for m in miss_samples[:20]:
            print(f"    - {m['award_id']}  parent={m['parent'] or '-'}")


if __name__ == "__main__":
    main()
