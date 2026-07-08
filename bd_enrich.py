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


def fetch_record(session, api_key, award_id):
    """Return (record, kind) for a government Award ID, trying contract then IDV."""
    for kind, path in (("contract", "/contract/"), ("idv", "/idv/")):
        try:
            r = session.get(
                BASE + path,
                params={"award_id": award_id, "api_key": api_key, "page_size": "1"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"    ! {kind} lookup failed for {award_id}: {e}")
            continue
        if r.status_code != 200:
            # 403 with a bad key is fatal; report once and stop trying this id.
            if r.status_code in (401, 403):
                print(f"    ! HigherGov auth error ({r.status_code}): {r.text[:120]}")
                return None, None
            continue
        results = (r.json() or {}).get("results") or []
        if results:
            return results[0], kind
    return None, None


def enrich_record(record, kind):
    contacts = _contacts_from(record)
    incumbent = _awardee(record)
    pop_end = (record.get("period_of_performance_current_end_date")
               or record.get("ordering_period_end_date") or "")
    return {
        "contacts": contacts,
        "incumbent": incumbent,
        "vehicle": _vehicle_name(record),
        "agency": _agency_name(record),
        "setAside": record.get("type_of_set_aside") or "",
        "popEnd": pop_end,
        "hgPath": _full_url(record.get("path")),
        "source": kind,
        "dateEnriched": TODAY,
    }


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
    by_award = {}   # award_id -> enrichment dict, cached within this run
    added = 0
    attempted = 0
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
        if attempted >= MAX_PER_RUN:
            print(f"  Reached per-run cap of {MAX_PER_RUN}; remaining leads enrich next run.")
            break

        if award_id in by_award:
            data = by_award[award_id]
        else:
            attempted += 1
            record, kind = fetch_record(session, api_key, award_id)
            if kind is None and record is None:
                # Distinguish auth failure (stop) from simple no-match (continue).
                data = None
            else:
                data = enrich_record(record, kind) if record else None
            by_award[award_id] = data
            time.sleep(0.2)   # be polite to the API

        if data:
            enrichment[lead_id] = data
            added += 1

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(ENRICH_FILE, "w") as fh:
        json.dump(enrichment, fh, indent=2)
    print(f"  HigherGov enrichment: {attempted} looked up, wrote {added} new "
          f"(total {len(enrichment)}) to {ENRICH_FILE}")


if __name__ == "__main__":
    main()
