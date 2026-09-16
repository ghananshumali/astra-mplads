# 🛰️ ASTRA — Agentic System for Transparency & Risk Analytics (MPLADS)

**SIH 2026 · PS SIH26102 · Ministry of Statistics and Programme Implementation**

ASTRA is a multi-agent, decision-support platform that analyses **real MPLADS
data** — 107,145 work records from the official eSAKSHI corpus — to surface
**risk flags for human review**: unusual spending, cost overruns, duplicate
works, delayed projects and guideline deviations, synthesised differently for
four authorities: **MP · District Authority · State Nodal Authority · Ministry**.

> ⚖️ **Human-in-the-loop by design.** Every output is a risk score requiring
> review by the competent authority. ASTRA never accuses, adjudicates, or acts
> automatically against any MP, agency, or contractor.

---

## Dual-mode data ingestion

Live government endpoints can be slow or unavailable. ASTRA therefore resolves
its data source at run time and **can never be blocked**, while always using
authentic government data.

```
                 Real-time official data available?
                              │
                   ┌──────────┴──────────┐
                  YES                   NO / SLOW / PARTIAL
                   │                     │
            Live ingestion         Offline official CSVs
              (live.py)               (offline.py)
                   └──────────┬──────────┘
                              ▼
                     ONE canonical schema
                              ▼
              agents → orchestrator → dashboard
```

| Mode | Behaviour |
|---|---|
| `auto` *(default)* | Probes the live official interfaces under a strict time budget, then analyses the **most complete authentic corpus** available. The official CSV exports are the full national record, so they supply the work corpus; the live portal supplies the freshness check and the pre-2023 baseline. Falls through to live ingestion if those exports are absent. |
| `live` | Live interfaces only — proves the real-time path works end to end. |
| `offline` | Official CSV exports only — deterministic and demo-safe. |

```bash
python scripts/fetch_data.py                 # auto
python scripts/fetch_data.py --mode live     # live only
python scripts/fetch_data.py --mode offline  # official CSVs only
```

### Live sources (all free / open, no paid keys)

| Source | What it provides | Status |
|---|---|---|
| **MPLADS eSAKSHI portal** `mplads.mospi.gov.in/rest/PreLoginDashboardData` | Live national totals from the **official MoSPI portal** → Ministry KPIs + **freshness check** | ✅ verified live |
| **data.opencity.in CKAN** | **Pre-2023** constituency records (15th/16th/17th LS + RS) → the historical-era baseline | ✅ verified live, cached after first pull |
| **api.empoweredindian.in** | Open (AGPL) eSAKSHI mirror, work-level records for live mode | ✅ verified live |
| **data.gov.in OGD API** | Resource-driven datasets (`config/sources.yaml`) | client included |

### Offline sources — official eSAKSHI exports in `datasets/`

| File | Rows | Contributes |
|---|---|---|
| Works Recommended | 107,146 | base record for every recommended work |
| Works Sanctioned | 79,245 | sanction date, sanctioned amount, workflow status |
| Works Completed | 34,458 | completion date, amount disbursed |
| Expenditure on Completed and On-going Works | 84,209 | payment tranches **with vendor name** → contractor network |
| Allocated Limit for Hon'ble MPs | 544 | per-MP entitlement → utilisation denominators |
| Amount consented for Calamity | 13 | calamity consents (excluded from cost baselines) |

Works are joined on the eSAKSHI code embedded in the work string
(`WS/MP<mp>/<FY>/<serial>-<work type>`); measured join coverage is **99.5–100%**.

### Freshness check — provable currency

During ingestion ASTRA queries the live MoSPI portal and compares totals:

```
local corpus 107,145 recommended works  ·  live portal 107,192  →  99.96% coverage (18th Lok Sabha)
```

This is shown in the dashboard banner, so an evaluator can see at a glance that
the analysed data is real and current. The live figure ticks upward between runs
(107,182 → 107,187 → 107,192 during this build) — which is itself the proof that
the portal is being queried live rather than replayed.

---

## Architecture

```
   live.py ─┐                    ┌─ Compliance   (deterministic, clause-cited)
   offline.py┤→ router.py →  ORCHESTRATOR ─┼─ Anomaly      (robust-z + IsolationForest)
   documents.py┘  (dual-mode)   (routing,  ├─ Entity-Res   (TF-IDF + evidence gate + geo)
                                 scoring,  └─ Network      (vendor/agency graph)
                                 narrative,
                                 tier synthesis)
                                     ↓
                    FastAPI tier endpoints · Streamlit 4-tier dashboard
                                     ↓
                     human review → feedback → threshold recalibration
```

**Why agentic, not a static pipeline:** the orchestrator inspects each batch and
routes around agents whose inputs are absent, recording the decision (visible in
the *Orchestration trace* tab). A rule that cannot be evaluated **stands down and
says why** rather than guessing — see R-SCST-01 below.

## The agents

The six analysis modules are deterministic code — rules and statistics — not
LLM agents. The optional LLM layer only rewrites their evidence for each
authority (see *AI synthesis layer*).

| Agent | Method | Output |
|---|---|---|
| **Ingestion** | Dual-mode router; era tagging around the 2023-04 eSAKSHI cutover; pdfplumber pipeline for utilisation certificates | canonical `works` + `fundflows` |
| **Compliance** | Deterministic rules, thresholds in `config/rules.yaml`, each finding citing its paragraph of the MPLADS Guidelines 2023 | paragraph-cited findings |
| **Statistical Anomaly** | Robust z-score (median/MAD) within *state × work-type × era* peer groups (empirical SoR proxy), materiality floor, degenerate-group and scale-mismatch handling; a peer profile across time to sanction, time to complete, share paid and payment count. Isolation Forest only corroborates | cost, expenditure and peer-profile outliers |
| **Entity-Resolution** | Blocked top-k sparse TF-IDF neighbours + rapidfuzz + **shared-rare-token evidence gate** + numeric-locator discriminator + geo gate find candidates; the record decides: a separating detail clears a pair, a batch of identical works is one held item, anything else is held until evidence | held duplicate pairs and batches (outside the score) |
| **Network** | Vendors (by portal vendor id), implementing agencies and district authorities: district spread, overrun concentration, national-supplier down-weighting | concentration signals |
| **Revision** | Edits the poller observed on the portal after sanction; normal progress is never a revision | post-sanction change signals |
| **Payment** | Every payment record the portal lists, read against the work's sanction and completion dates | payment-record signals |
| **Orchestrator** | Routing, severity-weighted composite score (0–100), data confidence, causal narrative, per-tier reframing | flags with narratives + 4 tier views |

## Detection rules (all on real data)

Every rule names its paragraph of the **MPLADS Guidelines, 1 April 2023**
(indexed in `config/guidelines_2023.yaml`) and its basis: a **scheme rule**, a
**screening** indicator for a rule the data cannot check directly, or ASTRA's
own **indicator**, which never claims to be a scheme rule.

| Rule | Basis | What it catches |
|---|---|---|
| `R-TIME-01` | 3.2.12 | Completion time beyond the one-year norm (18 months is ASTRA's escalation marker) |
| `R-PROH-01` | 5.2.1–5.2.14 | Works not permitted, each finding citing the specific paragraph |
| `R-COST-01` | 3.2.9 | Sanctioned below the **₹2.5 lakh** normal minimum (allowed with recorded reasons, so a prompt to check) |
| `R-SANC-01` | 3.2.4 | Recommendation awaiting sanction or rejection beyond 45 days |
| `R-SANC-02` | 3.2.4 (screening) | District authority whose sanctions typically take longer than 45 days |
| `R-REPAIR-01` | 5.1.9 | Repair and renovation above ₹50 lakh in a year for one MP (portal category, or described as repair) |
| `R-TRUST-01` | 6.2.6.2 | Works for societies and trusts above ₹50 lakh in a year for one MP (portal category) |
| `R-SCST-01` | 5.4.1 | SC/ST **area** allocation minima (15% / 7.5%) — stands down: no area data |
| `R-SCST-02` | 5.4.1 (screening) | SC/ST reserved-seat spend share |
| `R-SPIKE-01` | indicator (10.4.4) | Spending surge after dormant years, on completed financial years only |
| `R-PILE-01` | indicator (10.4.4) | Chronic under-use of entitlement, on completed financial years only |
| `A-COST-01` | statistical | Cost anomaly vs peer benchmark (SoR proxy, 3.2.13) |
| `A-PEER-01` | statistical | Extreme on two or more of time to sanction, time to complete, share paid, payment count |
| `A-EXP-01` | statistical | Expenditure-pattern outlier |
| `D-DUP-01` | statistical | Works that may be one work recorded twice: held, outside the score, until evidence decides |
| `D-DUP-02` | indicator | Repeated low-detail descriptions in one district |
| `N-NET-01` | indicator | Vendor, implementing-agency or district-authority concentration |
| `V-REV-01` | 3.2.15 | Work or site changed after sanction |
| `V-AMT-01` | indicator (3.2.3) | Amount revised after sanction |
| `V-IA-01`, `V-VEN-01` | indicator | District authority or main vendor changed after sanction |
| `V-PAY-01`, `V-LIST-01` | indicator | Payments reversed; paid work no longer listed (3.2.19) |
| `V-FREQ-01` | statistical | Revised unusually often — stands down until 90 days of history |
| `P-SEQ-01` | indicator | Payment dated before the work was sanctioned (none found so far) |
| `P-LATE-01` | 11.2 (screening) | Payments more than 180 days after the work was recorded as completed |
| `P-DUP-01` | indicator (10.7.1) | Same amount paid to the same vendor on the same day more than once on one work |

Common deviations the guideline itself allows with recorded reasons (`R-COST-01`
and `R-SANC-01` on a single work) are shown on a case that exists for another
reason, carry no score, and are otherwise summarised once per district
authority, so they cannot bury the review queue. Provisions no field lets ASTRA
check (for example SC/ST areas, the ₹1 crore a term per society or trust, calamity works) are listed
with the reason on the *Pipeline* page and in every analysis run record.

### Methodological honesty (what judges will probe)

These were found and fixed by testing against the real corpus, and they are the
difference between a demo and a system an authority could trust:

- **Prohibited-category matching had to learn what is being BUILT.** Naive
  keyword matching flagged 5,498 works as religious/prohibited; ~95% were
  landmarks — "Construction of Community Hall **near** Mallikarjun Temple" is a
  community hall. The rule now tests only the head asset phrase following the
  construction verb, vetoes on any permissible asset noun (including
  transliterated Hindi/Gujarati — *sadak, nali, kharanja, kuva*), recognises
  Indic locative postpositions (*ke pas*, *pase*, *se … tak*), and knows that
  *Shishu Mandir* and *Vidya Mandir* are schools. 5,498 → 255 asset findings.
  On the scheme's most politically sensitive rule, a missed flag is far cheaper
  than a false accusation.
- **R-SCST-01 stands down on this data.** The 15%/7.5% obligation concerns SC/ST
  *areas*; the published extracts only reveal whether a *seat* is reserved.
  Deriving "SC spend" from seat reservation marked every general constituency as
  0% and fired the rule against the whole country — a false positive on a
  politically sensitive metric. The rule now requires genuine area attribution
  and reports itself as skipped; `R-SCST-02` is the honest screening proxy.
- **Duplicate detection needs shared *rare* tokens.** MPLADS descriptions are
  templated, so text similarity alone produced 17,643 "duplicates" that were
  really distinct street-light works. A pair must now share several rare
  identifiers (place names, ward numbers), and **differing numeric locators**
  (culverts at KM 0+400 vs KM 1+200) are treated as evidence *against*
  duplication.
- **Matching text finds candidates; the record decides.** On 16 Sep 2026, 4,459
  of the 4,522 duplicate alerts came from a member recommending three or more
  works with one identical description (benches, high-mast lights, borewells for
  different villages), and payment records showed such batches paid to different
  gram panchayats. Now a detail that separates two works clears the pair (numbers
  or place names only one description carries, amounts at least 10% apart,
  different panchayats or municipalities paid); a batch of identical works is one
  held item per work; any other match is held at low severity, outside the risk
  score, until evidence such as the works' photos decides. Ids, dates, letter
  numbers, stages and vendors never clear a pair. Measured on copies of the live
  database the same day: alerts 5,791 → 611, and no work newly became an alert.
  The photo comparison itself is not built yet.
- **Cost anomalies need a materiality floor.** 17% of peer groups are degenerate
  (costs concentrated at one value, MAD = 0), so those use a percentile rule.
  A flag also requires ≥30% and ≥₹1 lakh above benchmark.
- **Scale mismatch is not an overrun.** A work >20× its peer median usually means
  the peer group mixes unit-priced and bulk works; it is reported as a
  *classification* question, not a cost allegation.
- **Term aggregates never enter annual time series.** Pre-2023 rows are published
  per Lok Sabha *term*; mixing them into a year-on-year series would invent
  dormancy and spikes.
- **The right edition of the guidelines.** Some government mirrors serve the
  June 2016 guidelines under a 2023 file name. The per-work minimum is
  **₹2.5 lakh** under the 1 April 2023 guidelines (para 3.2.9); ₹1 lakh was the
  2016 figure (para 3.26), which an earlier revision of this project had
  "confirmed" against the mislabelled copy. Checking every rule against the 2023
  text on 13 Sep 2026 also showed that repair and renovation are permitted
  (para 5.1.9, capped at ₹50 lakh a year), that government office buildings are a
  permitted category, and that the SC/ST provision is para 5.4.1. 993 findings
  that flagged permitted works were removed.
- **The data contract.** `astra/data_contract.py` records how the portal's fields
  were built and re-measures the facts that could change on every run: a work is
  complete when the Works Completed report lists it (most read "Physical
  Inspection", not "Work Completed"); `ia_name` is the district authority, and
  the agency executing a work is `implementing_agency`, which only payment
  records carry; recommended and sanctioned amounts have been identical on
  every work; the financial year in a work code is the year of **sanction**
  (on all 100,705 coded works; 29,493 were recommended in an earlier year), so
  the per-year limits on what an MP recommends (repair and renovation,
  societies and trusts) count by recommendation date; fund-series rules use
  completed years only and currently stand down.
- **A vendor is an id, not a name.** Grouped by name, 13 different vendors
  called "Ajay Kumar", 12 of them working in one state each, became one vendor
  "across 7 states" and a network case. The portal's payment records carry each
  vendor's id; 1,424 names belong to more than one id. Vendors are now grouped by
  id, and a case says how many other vendors share the name.
- **Fields the portal sends are used.** The portal's own work category (repair
  and renovation; trust and society), the recommendation letter, the member's
  term, the implementing agency and the vendor id were in every response but
  not stored. A database from before they were kept fills them from the raw
  response cache on the poller's next start, with no portal request and no
  entry in the edit history.
- **Every payment is kept, not just the total.** The portal lists each payment
  (date, amount, vendor, status) but gives it no id. All of them are stored
  (111,074 on 16 Sep 2026) and add up exactly to each work's total paid; the case page shows them
  as a timeline. On 16 Sep 2026 none was dated before its work's sanction and no
  work was paid more than its sanctioned amount. 975 sets of records were
  identical but for the row number; the portal counts each, so ASTRA keeps each
  and asks for the payment orders instead of assuming a duplicate. Several
  vendors on one work (4,241 works) and payments at the end of March are not
  treated as signals: the first is how materials and labour are bought, and
  funds do not lapse, so there is no year-end rush to find nationally.
- **Data confidence.** A finding on a work whose area is out of parity with the
  portal, failing to read, or awaiting confirmation of removals is marked
  *reduced confidence* with the reason, never hidden.

## Era awareness (eSAKSHI regime change)

Every record carries an `era` tag; **anomaly peer groups and baselines never
straddle the 2023-04 boundary**, so the regime change cannot masquerade as an
anomaly wave. Cross-era *duplicates* are deliberately hunted (migration
double-entry). The offline corpus is entirely post-2023; the pre-2023 rows come
from the live open-data interfaces, which is what makes era separation real
rather than hypothetical.

## Measuring the detectors

There are no confirmed labels for MPLADS irregularities, so real-world accuracy
cannot be measured, and ASTRA does not claim a figure. Two tools measure what can be:

```bash
python scripts/evaluate_detectors.py      # planted cases in real works: detection rate per rule, projected to 1M records
python scripts/review_sample.py export    # a sample of real findings for reviewers to mark
python scripts/review_sample.py score data/processed/calibration/review_YYYYMMDD.csv
```

`evaluate_detectors.py` plants known patterns into real works (a religious asset,
cost at 2–10× its peers, a duplicate entry, a post-sanction edit, and so on), runs
the full analysis, and reports caught and missed cases with 95% intervals, the
finding volume per million records, and fitted run time and memory. Planted cases
are cleaner than real ones, so the rates are upper bounds. `review_sample.py`
turns reviewers' verdicts on a stratified sample into precision per rule, which is
what any threshold change should rest on.

Every analysis run is recorded in the `analysis_runs` table: rules-file hash,
guidelines edition, code version, per-rule counts and stand-down reasons, and the
data-contract facts measured on that corpus.

## Quickstart

**1 · Backend (once)**

```bash
python -m pip install -r requirements.txt
python scripts/fetch_data.py        # dual-mode ingestion (auto)
python scripts/run_pipeline.py      # all agents + orchestrator
```

**2 · Run the poller, the API and the site**

```bash
powershell -ExecutionPolicy Bypass -File run_dev.ps1   # Windows
bash run_dev.sh                                        # macOS / Linux
```

This starts all three; Ctrl+C stops all three. Add `-NoPoller` (Windows) or
set `ASTRA_NO_POLLER=1` (macOS / Linux) to start only the site.

Or in separate terminals:

```bash
python -m astra.ingestion.poller                   # keeps data/astra.db current
python -m uvicorn astra.api.main:app --port 8000   # API   → :8000/docs
cd frontend && npm install && npm run dev          # React → :5173
```

Run the poller in its own terminal to keep the data updating while the site is
closed. Only one poller can run against a database: a second one prints who
already holds it and exits, and `run_dev` then uses the one already running.

What the poller does, and the settings that change it (environment variables):

| When | What | Setting |
|---|---|---|
| Every minute | Checks the portal's counts and re-reads only the areas whose counts moved | `ASTRA_POLL_INTERVAL` (seconds) |
| Every minute, 08:00–20:00 | In a quiet minute, also re-reads the one area read longest ago (five requests), so edits that change no figure arrive within hours | `ASTRA_ROLLING_AREAS` (`0` = off), `ASTRA_ROLLING_HOURS` (`HH:MM-HH:MM`, `always`, `off`) |
| Nightly, 03:00 | Re-reads every area record by record | `ASTRA_RECONCILE_AT` |
| After data changes | Recomputes the risk flags (about two minutes), at most every 3 hours and after each nightly check; review decisions are kept | `ASTRA_ANALYSIS_EVERY_MIN` (`0` = off) |
| When the portal stops answering | Pauses (the wait doubles up to 30 minutes) and serves the last good data; tries one national check every 5 minutes so a recovery is noticed soon. The Data source page shows when it failed, the last error and the next attempt; paused minutes are not counted as failures | `ASTRA_PORTAL_TRIAL_SECONDS` (`0` = no trials) |

To recompute the flags straight away, stop the poller and run
`python -m astra.ingestion.poller --analyse`.

Open **http://localhost:5173**. The header badge shows whether the data is live
and when the portal was last checked; the Data source page shows the latest
changes picked up from the portal. Point the client at a different API with
`VITE_API_BASE` in `frontend/.env`.

**3 · Keep it running unattended (Windows)**

`run_dev.ps1` is for working on the code: it stops when its window closes, and
nothing restarts a process that crashes. For a machine that should keep the
data current on its own, run the supervisor instead:

```bash
python -m astra.ops.supervisor                     # API, poller and website; Ctrl+C or --stop ends all
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -StartNow   # start at every sign-in
python -m astra.ops.supervisor --status            # processes, last backup, open alerts
python -m astra.ops.supervisor --stop              # stop it (do this before run_dev.ps1)
```

| What | How | Setting |
|---|---|---|
| Starts everything | API on :8000, poller, website on :5173; output in `data/logs/<name>.log` (rotated at 10 MB) | `ASTRA_API_PORT`, `ASTRA_WEB_PORT`, `--no-web`, `--no-poller` |
| Restarts what stops | After 5 s, doubling to 5 min while it keeps failing. A poller refused because another already holds the database is left alone. The supervisor's processes end with it, however it ends (Windows job object) | — |
| Starts at sign-in | `scripts/install_autostart.ps1` registers a Task Scheduler task for your account (no administrator rights) that also restarts the supervisor if it fails; `-Uninstall` removes it | — |
| Daily backup | A consistent copy of `data/astra.db` taken while everything runs, checked (`PRAGMA quick_check`) before it gets its name, newest 7 kept in `data/backups/`. `python -m astra.ops.backup --now` / `--list` / `--restore FILE` (restore refuses while ASTRA runs and keeps the replaced file) | `ASTRA_BACKUP_AT` (`02:30`, `off`), `ASTRA_BACKUP_KEEP`, `ASTRA_BACKUP_DIR` |
| Alerts | A Windows notification when something has stayed wrong past its grace period (API or website not answering 5 min, portal checks stopped 10 min, the portal failing 30 min, a check failing repeatedly 30 min, an area differing from the portal 2 h, nightly check or analysis failing, backup failed or older than 36 h, disk under 5 GB); repeated every 12 h while it lasts, and again when it clears. Every alert is also written to `data/logs/alerts.log` | `ASTRA_ALERT_CHANNELS` (`log,toast`), `ASTRA_ALERT_REMIND_HOURS` |
| Alerts on a phone | Optional push through [ntfy](https://ntfy.sh) (open source, no account). Off by default because the alert text goes to that server; pick an unguessable topic or run your own server | `ASTRA_ALERT_NTFY_TOPIC`, `ASTRA_ALERT_NTFY_SERVER`, add `ntfy` to `ASTRA_ALERT_CHANNELS` |
| Staying awake | Updates stop while the laptop sleeps. Optionally the supervisor asks Windows not to sleep while it runs on mains power; no power setting is changed | `ASTRA_KEEP_AWAKE=1` |

What it cannot do: nothing updates while the machine is off, asleep or offline,
and it cannot make the portal answer. It makes sure those situations are noticed
and recovered from without someone watching. `GET /meta/ops` serves the same
status to the site.

The original Streamlit dashboard remains at `dashboard/app.py`
(`streamlit run dashboard/app.py`) as a fallback; the React client is the
primary interface.

## AI synthesis layer (optional, Groq)

The LLM sits at the **end** of the pipeline and never touches detection. What is
deterministic stays deterministic:

```
                DETECTION AND RISK ASSESSMENT          ← rules, statistics, agents
   compliance · anomaly · entity-resolution · network
                          ↓
              deterministic composite risk score       ← never LLM-influenced
                          ↓
                RBAC CONSTRAINT ENGINE                 ← astra/rbac.py, pure Python
       role × risk level × evidence present  →  allow-list
                          ↓
                 GROQ LLM SYNTHESIS LAYER              ← astra/synthesis.py
   explanation · authority framing · action sequencing
                          ↓
                  validated + re-grounded              ← guardrails below
                          ↓
                    HUMAN DECISION                     ← the authority decides
```

The model receives a **compact evidence packet** (one case, agent findings,
measured values and benchmarks, the role, and the allow-list) — never the raw
dataset. It returns a strict JSON object.

### Guardrails — enforced in code, not just in the prompt

| Risk | Control |
|---|---|
| Model invents an action | Plan items carry only `action_id` + `reason`; ids are validated against `rbac.allowed_actions()` and anything else is dropped |
| Model exceeds its authority | The allow-list is computed per role, so a Ministry action offered to a District plan is rejected |
| Model changes the risk score | Score and band are copied from the pipeline **after** generation, overwriting anything the model said |
| Model fabricates figures | Every number is checked against the evidence packet; unmatched values are surfaced as unverified |
| Model alleges wrongdoing | Output is scanned for accusatory vocabulary, in English and Hindi; a hit discards the response and falls back |
| Model says nothing | A reply with an empty summary or explanation falls back; an action it chose but did not explain cites its finding |
| Model answers in the wrong language | A Hindi request answered in English falls back to the Hindi deterministic brief |
| Model unavailable | Any failure (no key, timeout, 429, bad JSON, network) returns the deterministic synthesis |

Punitive actions do not exist in the catalogue at all — no suspension, penalty,
blacklisting or criminal referral — so they cannot be recommended even if a
model asked for them.

### Same evidence, different authority, different permitted actions

The RBAC engine produces genuinely disjoint action sets, gated three ways:

| Authority | Permitted actions (for a 100/100 case with delay + cost + duplicate evidence) |
|---|---|
| **MP** | request status update · seek clarification · monitor progress · raise with authority |
| **District** | verify documents · review expenditure · cross-check duplicate · request progress report · compare with benchmark · mark under review · *inspect (risk ≥ 40)* · *escalate (risk ≥ 60)* |
| **State Nodal** | request district review · compare across districts · prioritise for monitoring · *seek district report (≥ 40)* · *refer systemic pattern (≥ 60)* |
| **Ministry** | monitor national pattern · review rule threshold · prioritise systemic issue · *request state review (≥ 40)* · *consider guidance update (≥ 60)* |

Gating is by **role**, by **risk** (a low-risk case is never offered escalation
or inspection) and by **evidence** (`cross_check_duplicate` only appears when
the entity-resolution agent actually found a match).

### Enabling it

```bash
cp .env.example .env          # then paste a free key from console.groq.com/keys
# .env is git-ignored; the key is read server-side only and never reaches the browser
```

`GROQ_API_KEY=` empty or absent is a fully supported state: the platform runs
exactly as before on the deterministic synthesis layer, and the UI says which
layer produced each result. Model defaults to `openai/gpt-oss-20b`
(strict JSON-schema constrained decoding, fastest Groq production model);
override with `ASTRA_GROQ_MODEL`. No extra dependency — the OpenAI-compatible
REST endpoint is called with `requests`. gpt-oss is a reasoning model and is
asked for low reasoning effort (`ASTRA_LLM_REASONING_EFFORT`): at the default
effort it can spend its whole token budget thinking and return empty fields.

## The interface — a React risk investigation workspace

The frontend is a Vite + React + TypeScript application in `frontend/`. It is a
pure client of the FastAPI backend: no risk scores, permissions or synthesis are
computed in the browser.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ASTRA  MPLADS Risk Intelligence      [AI synthesis] [OFFLINE 99.9%] [MP▾] │
├───────────┬──────────────────────────────────────────────────────────────┤
│ Overview  │  EXECUTIVE OVERVIEW                                           │
│ Risk cases│  works · cases · high risk · under review · closed  (clickable)│
│ Geography │  states by risk │ what is being detected │ priority queue      │
│ Network   ├──────────────────────────────────────────────────────────────┤
│ Pipeline  │  CASE TABLE                │  CASE INVESTIGATION              │
│ Data      │  search · filters · sort   │  Why flagged │ AI synthesis │    │
│           │  risk · title · location   │  Agent trace │ Evidence │        │
│           │  signals · status          │  Work record │ Duplicates        │
│           │  pagination                │  + human review decision         │
└───────────┴────────────────────────────┴──────────────────────────────────┘
```

**Design system.** Tokens in `frontend/src/styles/tokens.css`: a navy
government palette, one accent, and a **fixed severity ramp** that means the
same thing on every surface — a chart bar, a table chip and a map marker all
use the same red for high risk. Chart series colours are deliberately drawn
from a separate palette so a category colour is never mistaken for a risk level.

**Authority experience.** The switcher in the header changes both the data scope
and the synthesis. Each tier remembers its own scope selection, and the case
list, overview charts and action plans all re-derive from the backend for that
role — Ministry sees state rankings and national detection mix, State Nodal sees
cross-district comparison, District and MP see their own scoped queue.

**Interactions that do something.** KPI tiles, chart bars, detection rows, map
markers and district rows all drill into a pre-filtered case list. The case
table supports debounced search, risk/status/detection filters, sorting and
pagination; selecting a case opens the investigation panel without leaving the
list, and the list widens when nothing is selected.

### Frontend architecture

```
frontend/src
  api/client.ts        single service layer; every backend call goes through it
  api/types.ts         TypeScript mirror of the FastAPI contract
  state/               authority (RBAC role) context, persisted per tier
  components/ui/       Card, Chip, RiskBadge, Banner, Empty, Skeleton, Tabs…
  components/shell/    header, authority switcher, sidebar navigation
  components/case/     the case investigation panel and its tabs
  pages/               Overview · Cases · Geography · Network · Pipeline · Data
  lib/format.ts        presentation helpers + the shared risk/severity mapping
  lib/centroids.ts     state centroids, generated from the Python source
```

### Languages — English and Hindi

The header's language menu switches the whole site between English (the
default on every first visit) and हिन्दी; the choice is remembered on that
device. The interface text lives in `frontend/src/i18n/locales/`. Everything the
backend writes about a case — why it was flagged, the agent trace, evidence
explanations, guideline clauses, the action plan's reasons, the pipeline page's
notes — comes from templates in `config/locales/en.yaml` and `hi.yaml`
(`astra/locale.py`), filled with the values the agents produced. The case
endpoints, `/cases` and `/meta/pipeline` take `lang=en|hi`.

English is what the analysis stores and is served unchanged. Hindi is rendered
on request from the same stored findings, so it states exactly the same figures
and paragraph numbers; `tests/test_case_language.py` checks that on every
template and on stored cases. The AI synthesis is asked to write in Hindi from
the Hindi evidence, under the same guardrails plus Hindi forbidden vocabulary.
What stays as recorded: work descriptions, names, places, portal stage values,
and each agent's own statement in the Evidence tab's audit record. The Hindi
text is a draft awaiting review by native speakers.

### Endpoints added for the client

CORS, plus read-only pass-throughs that expose data the previous Streamlit app
read directly from `astra.db`. None of them contain business logic:

| Endpoint | Wraps |
|---|---|
| `GET /cases` | `db.query_flags` + `db.count_flags` (filter, sort, paginate) |
| `GET /stats` | `db.flag_stats` + corpus counts |
| `GET /meta/facets` | `db.flag_facets` + rule coverage |
| `GET /analytics/states` · `/districts` · `/detections` | the matching `db` summaries |
| `GET /works/{id}` · `/agencies/works` | canonical work rows |
| `GET /meta/pipeline` | the last run's router trace and rule coverage |
| `GET /payments?work_id=` | a work's payment records, each marked against its dates |
| `GET /meta/ops` | the supervisor's processes, the last backup and open alerts (`astra.ops`) |

The pre-existing endpoints (`/flags/{tier}`, `/flags/case/{id}`, `/synthesis`,
`/actions`, `/feedback`, `/meta/*`) are unchanged.

## Demo journey

1. **Ministry view** — national overview: 107,145 works analysed, 242 high-risk
   cases, states ranked by risk, what is being detected, geographic concentration.
2. Pick a high-risk case from the list and click **Investigate →**.
3. **Why flagged** — primary risk, supporting signals, recommended actions, all
   in plain language.
4. **Agent trace** — the four agents, their measurements and benchmarks, their
   individual risk contributions, feeding the synthesiser.
5. **Evidence** — the exact numbers and the raw audit trail behind each signal.
6. **Duplicates** — this work once, its batch of identical works (amount, who was
   paid, stage, letter; works paid to the same panchayat marked), and the matched
   works held for evidence, side by side.
7. **AI synthesis & action plan** — the evidence-grounded explanation plus a
   staged action plan (IMMEDIATE → NEXT → IF CONCERNS PERSIST → ESCALATION),
   with the deterministic allow-list shown beneath it.
8. **Switch authority to District Authority** — the same case and same evidence,
   reframed *and* given a different permitted action plan. *Same evidence →
   different authority → different permitted actions.*
9. **Escalate** or **Close as false positive** — the status updates immediately.

## Repo map

```
config/rules.yaml          # every threshold + guideline citation (tunable, no code changes)
datasets/                  # official eSAKSHI CSV exports (offline mode)
astra/ingestion/router.py  # dual-mode resolution + provenance
astra/ingestion/live.py    # eSAKSHI portal, CKAN pre-2023, open mirror
astra/ingestion/offline.py # official CSV -> canonical schema
astra/agents/              # compliance, anomaly, entity_resolution, network, orchestrator
astra/explain.py           # plain-language layer: titles, signals, per-tier briefs
astra/locale.py            # case text in English or Hindi, from config/locales/*.yaml
astra/rbac.py              # deterministic action catalogue + constraint engine
astra/synthesis.py         # evidence packet -> LLM -> validation -> fallback
astra/llm/provider.py      # isolated Groq client (server-side, never raises)
astra/db.py                # SQLite + indexed query layer powering the dashboard
astra/ops/                 # unattended running: supervisor, backups, health, alerts
astra/api/main.py          # tier endpoints, /flags/case/{id}[/synthesis|/actions],
                           # feedback, provenance, /meta/llm
frontend/                  # React + Vite + TypeScript client (primary UI)
dashboard/app.py           # original Streamlit UI, kept as a fallback
tests/test_system.py       # 142 end-to-end checks against the real corpus,
                           # including RBAC and LLM-guardrail tests
tests/test_frontend_api.py # 51 API contract tests for the React client
run_dev.ps1 / run_dev.sh   # start API + frontend together (development)
scripts/install_autostart.ps1  # start the supervisor at Windows sign-in
```
