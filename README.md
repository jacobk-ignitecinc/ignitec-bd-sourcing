# Ignitec BD Sourcing Pipeline

Automates the crawl layer of Ignitec's BD sourcing and feeds the BD Sourcing
Cockpit. A scheduled GitHub Action crawls federal opportunity data, enriches it
with contacts and incumbents, summarizes each one, and commits cockpit-ready
JSON. The cockpit (in `docs/`, served via GitHub Pages) is where the team
triages leads and runs a pursuit pipeline.

For crawler internals, guardrails, and the lead schema, see `CLAUDE.md`.

## The cockpit (two tabs)

- **Sourcing** — every capability-aligned recent award and expiring contract,
  with AI summaries, HigherGov contacts, vehicle, and a link to the opportunity.
  Each is scored 0-100 and bucketed into **Tier A** (review first), **Tier B**
  (worth a look), or **Tier C** (long tail). The score blends capability-lane
  fit, experience affinity (agencies, NAICS, and primes Ignitec has actually
  won or subcontracted under), relationship/route, and opportunity fit (value,
  shaping window, set-aside); hover the tier chip to see the reasons. The list
  defaults to **Tier A + B**, sorted by score. Click **+ Pursue** on the ones
  worth going after or **✕ Reject** on the ones that are not a fit; by default
  only **undecided** opportunities show (the Decision filter can show All, In
  pipeline, or Rejected). Tiers and decisions are tunable in
  `config/ignitec.json` (`profile.experience`, `profile.scoring`) and shared
  through the pipeline file.
  Recent awards can be filtered by action age (0-30 / 30-90 / 90+ days) for the
  subcontracting play; expiring contracts by horizon (near-term 3-6mo, mid-term
  6-12mo, long-term 12mo+, plus an "imminent" tag for leads that have aged under
  3 months since an earlier crawl) and by set-aside (SB or 8(a) - useful because 8(a)
  incumbents graduating or contracts coming off 8(a) open a prime lane). Set-aside
  is sourced from HigherGov enrichment, since the USASpending award search does
  not return it. Each card has a "Find contact" row with pre-filled links to
  ZoomInfo, LinkedIn, SAM.gov (by UEI), and Google for the incumbent/awardee
  company, so a BD contact can be pulled with existing tools; no third-party
  contact data is stored in the repo.
- **Pipeline** — a Kanban board of only the pursued opportunities, with stages
  (Pursue → Researching → First contact → In discussion → Teaming/RFI →
  Submitted → Won/Lost/Parked), owners, next actions, and a timestamped
  activity log per opportunity.

## Shared pipeline setup (one-time, per person)

The pipeline lives in `output/pipeline.json` so everyone opening the dashboard
sees the same board. Reading it needs nothing. **Saving** changes needs a
GitHub token, because the dashboard writes the file through the GitHub API.
The token is stored only in your browser (localStorage) and is never committed.

Only people who will *save* changes need a token. Viewers do not.

### Create the token

1. Go to **GitHub → your photo (top right) → Settings → Developer settings →
   Personal access tokens → Fine-grained tokens**, or open
   <https://github.com/settings/personal-access-tokens/new>.
2. **Token name:** `Ignitec BD pipeline`. **Expiration:** 90 days is fine (you
   will regenerate when it lapses).
3. **Resource owner:** choose the account that owns the repo
   (`jacobk-ignitecinc`). If it is an organization and the repo does not appear
   in the next step, an org admin must approve the token request or enable
   fine-grained tokens for the org.
4. **Repository access:** select **Only select repositories** and choose
   `jacobk-ignitecinc/ignitec-bd-sourcing`.
5. **Permissions → Repository permissions → Contents:** set to
   **Read and write**. (Metadata read-only is added automatically. Nothing else
   is needed.)
6. Click **Generate token** and copy it. It starts with `github_pat_` and is
   shown only once.

### Use it

1. On the **Pipeline** tab, click **Connect GitHub** and paste the token.
2. Make your changes (pursue opportunities, move stages, log activities).
3. Click **Save to GitHub** to publish the board for the team. Saves merge the
   remote and your local copy per opportunity (most recently touched wins), so
   concurrent editors do not overwrite each other.

The **?** button next to Connect GitHub shows these same steps inside the app.

## Running the crawl

- Scheduled: the Action in `.github/workflows/bd-sourcing.yml` runs weekly and
  can be triggered by hand from the Actions tab (Run workflow).
- Secrets used: `SAM_API_KEY`, `HIGHERGOV_API_KEY`, `ANTHROPIC_API_KEY`.
- Local: `pip install -r requirements.txt`, then set the keys in the
  environment and run `python ignitec_bd_sourcing.py` (crawl),
  `python bd_enrich.py` (contacts/incumbent/vehicle),
  `python bd_summarize.py` (AI summaries), `python bd_report.py` (report).
