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

## The five agents

| Agent | Method | Output |
|---|---|---|
| **Ingestion** | Dual-mode router; era tagging around the 2023-04 eSAKSHI cutover; pdfplumber pipeline for utilisation certificates | canonical `works` + `fundflows` |
| **Compliance** | Deterministic rules, thresholds in `config/rules.yaml`, each finding citing its guideline provision | clause-cited findings |
| **Statistical Anomaly** | Robust z-score (median/MAD) within *state × work-type × era* peer groups (empirical SoR proxy), IsolationForest cross-check, materiality floor, degenerate-group and scale-mismatch handling | cost/expenditure outliers |
| **Entity-Resolution** | Blocked top-k sparse TF-IDF neighbours + rapidfuzz + **shared-rare-token evidence gate** + numeric-locator discriminator + geo gate | duplicate pairs with likely mode |
| **Network** | Vendor & implementing-agency graph: district spread, overrun concentration, national-supplier down-weighting | contractor concentration signals |
| **Orchestrator** | Routing, severity-weighted composite score (0–100), causal narrative, per-tier reframing | flags with narratives + 4 tier views |

## Detection rules (all on real data)

| Rule | What it catches |
|---|---|
| `R-TIME-01` | One-year completion norm breach (18-month outer limit) |
| `R-PROH-01` | Negative-list / prohibited work categories |
| `R-COST-01` | Below the **₹1 lakh** per-work minimum, or above the review ceiling |
| `R-SPIKE-01` | **Non-lapsable fund spike** — terminal-year surge after dormant years |
| `R-PILE-01` | Chronic under-utilisation (funds idling while entitlements accrue) |
| `R-SCST-01` | SC/ST **area** allocation minima (15% / 7.5%) |
| `R-SCST-02` | SC/ST reserved-seat spend share — screening proxy |
| `A-COST-01` | Cost anomaly vs peer benchmark (SoR proxy) |
| `A-EXP-01` | Expenditure-pattern outlier |
| `D-DUP-01` | Duplicate / near-duplicate work |
| `D-DUP-02` | Repeated low-detail descriptions in one district |
| `N-NET-01` | Vendor / agency concentration |

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
- **Cost anomalies need a materiality floor.** 17% of peer groups are degenerate
  (costs concentrated at one value, MAD = 0), so those use a percentile rule.
  A flag also requires ≥30% and ≥₹1 lakh above benchmark.
- **Scale mismatch is not an overrun.** A work >20× its peer median usually means
  the peer group mixes unit-priced and bulk works; it is reported as a
  *classification* question, not a cost allegation.
- **Term aggregates never enter annual time series.** Pre-2023 rows are published
  per Lok Sabha *term*; mixing them into a year-on-year series would invent
  dormancy and spikes.
- **Verified thresholds.** The per-work minimum is **₹1 lakh** (confirmed against
  the 2023 guidelines — an earlier ₹2.5 lakh assumption was wrong). Unverifiable
  paragraph numbers were removed rather than guessed.

## Era awareness (eSAKSHI regime change)

Every record carries an `era` tag; **anomaly peer groups and baselines never
straddle the 2023-04 boundary**, so the regime change cannot masquerade as an
anomaly wave. Cross-era *duplicates* are deliberately hunted (migration
double-entry). The offline corpus is entirely post-2023; the pre-2023 rows come
from the live open-data interfaces, which is what makes era separation real
rather than hypothetical.

## Quickstart

**1 · Backend (once)**

```bash
python -m pip install -r requirements.txt
python scripts/fetch_data.py        # dual-mode ingestion (auto)
python scripts/run_pipeline.py      # all agents + orchestrator
```

**2 · Run both services**

```bash
powershell -ExecutionPolicy Bypass -File run_dev.ps1   # Windows
bash run_dev.sh                                        # macOS / Linux
```

Or in two terminals:

```bash
python -m uvicorn astra.api.main:app --port 8000   # API   → :8000/docs
cd frontend && npm install && npm run dev          # React → :5173
```

Open **http://localhost:5173**. Point the client at a different API with
`VITE_API_BASE` in `frontend/.env`.

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
| Model alleges wrongdoing | Output is scanned for accusatory vocabulary; a hit discards the response and falls back |
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
REST endpoint is called with `requests`.

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
6. **Duplicates** — side-by-side text comparison of the two matching records.
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
astra/rbac.py              # deterministic action catalogue + constraint engine
astra/synthesis.py         # evidence packet -> LLM -> validation -> fallback
astra/llm/provider.py      # isolated Groq client (server-side, never raises)
astra/db.py                # SQLite + indexed query layer powering the dashboard
astra/api/main.py          # tier endpoints, /flags/case/{id}[/synthesis|/actions],
                           # feedback, provenance, /meta/llm
frontend/                  # React + Vite + TypeScript client (primary UI)
dashboard/app.py           # original Streamlit UI, kept as a fallback
tests/test_system.py       # 142 end-to-end checks against the real corpus,
                           # including RBAC and LLM-guardrail tests
tests/test_frontend_api.py # 51 API contract tests for the React client
run_dev.ps1 / run_dev.sh   # start API + frontend together
```
