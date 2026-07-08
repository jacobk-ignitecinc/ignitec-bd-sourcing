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
  Filter and review, then click **+ Pursue** on the ones worth going after or
  **✕ Reject** on the ones that are not a fit. By default the list shows only
  **undecided** opportunities (pursued and rejected ones are hidden); the
  Decision filter can show All, In pipeline, or Rejected. Decisions are shared
  through the same pipeline file.
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
