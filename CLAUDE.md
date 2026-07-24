# Ignitec BD Sourcing Pipeline — Project Guide for Claude Code

This repository automates Layer 1 (the crawl) of Ignitec's BD sourcing pipeline and
feeds the BD Sourcing Cockpit (a separate React artifact used in Claude.ai). Read this
file before making changes. The guardrails below are non-negotiable and exist to protect
Ignitec from audit and protest risk.

## What this project does
Crawls federal opportunity data and emits cockpit-ready JSON leads from three feeds:
1. USASpending recent awards (subcontracting targets)
2. USASpending expiring contracts (recompete shaping targets)
3. SAM.gov Sources Sought and Special Notices (the RFI cadence feed)
A GitHub Action runs the crawler on a schedule and commits new leads. The cockpit imports
the JSON, runs a five-gate go/no-go, and tracks status, posture, owner, and next action.

## Repo layout
- ignitec_bd_sourcing.py — the crawler (v0.2)
- requirements.txt — Python dependencies (requests)
- .github/workflows/bd-sourcing.yml — the scheduled job
- state.json — dedupe ledger, created on first run; do not hand-edit
- output/ — emitted JSON leads; output/latest_new_leads.json is the cockpit import file

## How to run and test
- Local: `pip install -r requirements.txt` then `SAM_API_KEY=... python ignitec_bd_sourcing.py`
- SAM_API_KEY must come from the environment or a GitHub secret. Never hard-code or commit it.
- After a run, output/latest_new_leads.json is the file to import into the cockpit.

## Validate against live APIs first (high priority)
The crawler was written without live API access, so verify and fix these before relying on it:
- USASpending: confirm the endpoint, filter keys, and field names ("End Date" is the current
  period-of-performance end) against https://api.usaspending.gov/docs/ . Contract and IDV
  award-type groups are queried separately on purpose.
- SAM.gov: confirm parameter names (postedFrom, postedTo, ptype, naicsCode) and notice-type
  codes (r = Sources Sought, s = Special Notice) against
  https://open.gsa.gov/api/get-opportunities-public-api/ . NAICS is also filtered client-side
  as a backstop.
- If a field is missing or renamed, fix the mapping. Do not silently drop fields.

## Lead schema (crawler output MUST match this or cockpit import breaks)
Keys: id, source, title, prime, agency, subAgency, value, awardId, solicitationNumber,
naics, psc, popStart, popEnd, posture, tier, lane, gates (array of five nulls),
status, owner, nextAction, nextActionDate, notes, dateAdded, lastTouched.
Also emitted (added after v0.2, all optional and preserved on cockpit reload):
setAside, vehicle (the parent IDV PIID), vehicleHeld, incumbent, routing, url
(public USASpending award page). AI summaries and HigherGov contacts/incumbent/
vehicle live in SEPARATE files (output/summaries.json, output/enrichment.json),
keyed by lead id, so the lead schema stays pure.
- source is one of: "Recent Award", "Expiring Contract", "RFI/Sources Sought",
  "Email/Referral", "Manual".
- posture strings must match the cockpit exactly: "Shape", "Position", "Compete/Team",
  "Warm - route via channel", "Cold outreach", or "".
- Keep ids deterministic (hash of the award ID or notice id) so re-runs never duplicate.

## Five-gate go/no-go bands (keep any scoring logic consistent with these)
Framework is five gates as of Playbook v1.4. Team Capacity is NO LONGER a scored gate;
capacity is filtered at the sourcing stage, so scoring it double-counts. A non-scored,
binary Feasibility Check runs before the gates instead.
Gates: 1 Core Lane Alignment, 2 Past Performance Defensibility, 3 Clearance and Compliance,
4 Competitive Position, 5 Effort vs Probability of Win. Each scored 0 to 3.
- 2.6+ pursue (prime or preferred sub); 2.2 to 2.5 pursue as sub; 2.0 to 2.1 conditional;
  below 2.0 no-bid.
- Any single gate at 0 is an immediate no-bid for a prime bid.
- Feasibility Check (run first, not scored, binary; any failure is an immediate no-bid):
  submission mechanics, pricing gate (Anand sign-off), mandatory artifacts, named personnel,
  legal restrictions. Do NOT score current workload or submission overlap.
- An RFI touching Tier 1 or Tier 2 responds regardless of score (RFI override).
- Staffing-primary override: for labor-hour/FTE work, assume candidates and LOCs can be
  secured during capture; sourcing difficulty alone is never a no-bid.

## Non-negotiable guardrails (truth-first)
- Never invent facts, metrics, past performance, clause citations, or contract data. Use
  bracketed placeholders such as [INSERT METRIC] where a value is unknown.
- Identifiers: UEI (KLHRNT5GPFH5) and CAGE (88CY6) only. Never use DUNS. DUNS 081557884 is
  retired (2022). Do not add it to any file, output, or generated document.
- Always label prime versus subcontractor roles explicitly. Never blur them.
- The $51.1M portfolio figure is cumulative realized revenue (2019-Apr 2026), never annual.
- Placement count is 960+ (2019-Jun 2026), not 1,529. The 1,529 figure double-counted a
  duplicate data tab and is retired. Active: 62 placements, 14 contracts and engagements,
  78 named customers, 590 unique individuals (per Consolidated Data through Jun 30, 2026).
- 8(a) is pending, not certified. Never claim it as active.
- Accounting is FAR Part 31 compliant. Do not claim DCAA-approved or DCAA-audited.

## Writing and formatting standards (for any generated document or proposal text)
- No em dashes. Use "and" not "&". Single space after periods.
- Active voice. Quantify claims. No superlatives (best, leading, world-class, cutting-edge).
- Customer-first framing: restate the agency mission in its own language before Ignitec's approach.
- Arial font; red (#C0222C) H1 with a bottom rule; navy (#1F3864) H2.
- File naming: [NoticeNumber]_[DocumentType]_IGNITEC-INC
  (for example CB26-RFQ0012_TechnicalApproach_IGNITEC-INC).
- Logos: use the .jpg versions on white documents.

## Likely tasks
- Validate and fix the crawler against the live USASpending and SAM APIs.
- Add a Fetch from URL path so the cockpit reads latest_new_leads.json from the repo raw URL.
- Build a hosted version that crawls on a schedule and pre-vets each hit with a five-gate read.
- Extend to proposal production: generate style-guide-compliant DOCX from templates with python-docx.

## Ask before doing
- Do not commit secrets. Do not populate pricing; Anand owns all rate decisions.
- Flag, do not silently change, anything that would alter a defensibility posture or a claim.
