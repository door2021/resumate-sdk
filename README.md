# Resumate

Runtime reliability & self-healing layer for AI agents (LangGraph first).
See `doc.md` for the full product spec.

## Changelog
Dated entries going forward, one per applied change - full explanation
always in `doc.md` at the section referenced here.

- **2026-11-08** - Recovery gate, step 2: the language-model layer of the
  failure classifier (`apps/repairs/llm_classifier.py`). Rules still classify
  first; only an error that no rule matches is sent to the one model the
  organization chose (OpenAI, Google Gemini or Groq, model name free text).
  The model picks one of four fixed categories (transient, correctable,
  deterministic, unknown) with a confidence and never chooses an action. It is
  asked twice in different wording: different categories, an off-list answer or
  an unreadable answer give "unknown", which the gate escalates (the check can
  only lower the result). Agreeing answers keep the lower confidence, capped at
  0.9. Provider errors and timeouts are "unknown" and are not saved. Answers
  are saved per organization, error signature, provider, model name and prompt
  version (`LlmClassification`) so a signature is never sent twice and other
  models can be compared offline on the same signatures. Each repair attempt
  records `classifier_source` (rules, llm, llm_cache) and `classifier_info`
  (provider, model, prompt version, both answers). Settings page: provider,
  model name, write-only API key (encrypted at rest with the existing Fernet
  key, never shown, logged or copied; owner/admin only). Error text is
  redacted (emails, secrets, long ids, numbers) and cut to 300 characters
  before it is sent; the key travels in a header, never in a URL. New settings:
  `RESUMATE_LLM_TIMEOUT_SECONDS` (15), `RESUMATE_LLM_CONFIDENCE_CAP` (0.9),
  `RESUMATE_LLM_MAX_ERROR_CHARS` (300). The three provider calls are tested
  against mocked responses only; check each once with a real key. 129/129
  tests passing (101 before, plus 28 new).

- **2026-11-05** - Recovery gate, step 1 of the server migration
  (`apps/repairs/gate.py`). `attempt_repair` now applies five rules in a fixed
  order: (1) a succeeded ledger receipt resumes with the recorded result;
  (2) a deterministic or unknown failure class, or the attempt limit,
  escalates; (3) an effectful step with a processing or unknown receipt, or an
  irreversible step with no receipt, is revalidated (no validator registered or
  an unclear answer escalates; "effect_done" resumes with the recorded result);
  (4) classifier confidence below the threshold for the step's effect type
  escalates (irreversible and undeclared steps have no threshold); (5) otherwise
  resume with a repair strategy. New: `effect_type` (none, repeat_safe,
  reversible, irreversible; blank = irreversible), `validator_registered` and
  receipt states processing/succeeded/failed/unknown on the checkpoint API
  (unknown values get a 400); `POST /api/v1/runs/<id>/revalidation/`; an
  `Escalation` model with approve, skip, stop and hold (admin actions); run
  statuses `escalated` and `held` (a held run is never resumed automatically);
  per-step attempt numbers with a unique constraint as the atomic claim; gate
  audit fields on `RepairAttempt`. Removed: guessing side effects from the
  "Tool:" name prefix. Existing clients that send no `effect_type` now escalate
  every failure; set `RESUMATE_UNDECLARED_EFFECT_TYPE=none` while migrating.
  `RESUMATE_STORE_STEP_DATA` (default on) keeps storing step output and the
  receipt result until the SDK keeps its own ledger. 101/101 tests passing
  (74 existing, updated where they relied on the old behaviour, plus 27 new).

- **2026-09-14** - Ledger docs corrected (SDK README, Integrations page)
  after two real gaps found via public feedback: `default_ledger()`
  doesn't survive a crash (reproduced directly), and timeouts are
  classified the same as confirmed failures with no "unknown" state.
  Audit trail (run detail page) now surfaces the raw ledger status for
  any failed step with a receipt - didn't exist before. 74/74 tests
  passing. See doc.md section 2.28.
- **2026-09-14** - Integrations page now shows the `pip install
  "resumate-sdk[ledger] @ ..."` install option (was undiscoverable before -
  the ledger integration is one of the product's two real differentiators)
  plus a `run_idempotent()` usage example. Verified by rendering the real
  page through Django's test client - 74/74 tests still passing. See
  doc.md section 2.27.
- **2026-09-14** - `.env.example` created (was referenced since this
  file's first version, never actually existed) with all six variables
  `config/settings.py` reads, format gotchas documented inline. Verified
  by running the app from `.env` alone, both sqlite and Postgres paths -
  74/74 tests passing. See doc.md section 2.26.
- **2026-09-14** - Database provider switched: Neon (free, no inactivity
  expiry, auto-resumes on next query) instead of Render's own Postgres
  (expires 30-90 days, not meant for production). `render.yaml` no longer
  provisions a database - `DATABASE_URL` is now set by hand in Render's
  dashboard after creating a Neon project. No app code changed. See
  doc.md section 2.25.
- **2026-09-14** - Real production deployment path built and verified:
  `render.yaml` (provisions free Postgres), `build.sh`, `gunicorn` added
  as an actual dependency. Fixed the `pyproject.toml` packaging bug so
  `pip install -e ".[dev]"` works again. Verified against a real local
  Postgres 16 (not assumed): migrations clean, 74/74 tests passing.
  Corrects an earlier claim that these files already existed and were
  verified - they didn't and weren't. See doc.md section 2.24.
- **2026-09-14** - Fixed: browser back-button after logout could show the
  stale dashboard from bfcache instead of redirecting to login (`never_cache`
  on `OrgScopedView`). While fixing this, found and fixed a real regression
  from the landing-page change below: 20 of 71 tests were failing on stale
  hardcoded paths. 74/74 tests passing. See doc.md section 2.23.
- **2026-09-14** - Public landing page added at `/` (`apps/landing`),
  dashboard moved to `/app/`. See doc.md sections 2.22-2.23 (2.22 also
  corrects an earlier, never-applied draft of this same feature).

## Repo layout

This is now two SIBLING folders, not one - `resumate-sdk` used to live inside
this repo but was split out into its own repo/directory once real design
partners needed to `pip install` it (a private repo can't be
`pip install git+...`'d by strangers, and nesting it inside this repo caused
its `pytest`/`uv` config to leak in from this repo's `pyproject.toml`).
See `../resumate-sdk/README.md` for the SDK itself.

    parent-folder/
    ├── resumate/               # THIS repo - Django backend, private, deploys to Render
    │   ├── apps/               # Django apps - the server (accounts, integrations, workflows, repairs, learning, dashboard, core)
    │   ├── config/              # Django settings, urls, asgi/wsgi
    │   ├── templates/           # Dashboard UI (Tailwind + Django templates)
    │   ├── manage.py
    │   └── pyproject.toml       # Django app's deps only - resumate-sdk has its own, separate pyproject.toml
    │
    └── resumate-sdk/           # SIBLING repo, not a subfolder of this one - public, uv-managed
        │                       # CLIENT-side code a customer installs into their own
        │                       # LangGraph agent process via `pip install git+https://...`
        └── pyproject.toml       # Own deps (just httpx) - deliberately has no Django dependency

## Local setup - Django app (the server)
**Fixed 2026-09-14:** `pip install -e ".[dev]"` previously failed - this
project has `[tool.uv] package = false` and now also a
`[tool.hatch.build.targets.wheel]` config in `pyproject.toml`, verified
working in a genuinely fresh venv. `uv sync --extra dev` remains the
recommended path (it correctly resolves `resumate-sdk`'s git-sourced
dependency in `[tool.uv.sources]`, which plain pip cannot). Also:
`.env.example` referenced below does not currently exist in this repo -
create a `.env` by hand with at minimum `RESUMATE_ENCRYPTION_KEY` (see
`config/settings.py` for every variable and its default).

    uv sync --extra dev          # or `pip install -e ".[dev]"` (fixed)
    cp .env.example .env  # then fill in RESUMATE_ENCRYPTION_KEY
    python manage.py migrate
    python manage.py createsuperuser
    python manage.py runserver

## Troubleshooting: "no such table: repairs_repairattempt" (or any app)
Means a migration file that exists in this repo didn't make it into your
local copy - check with `python manage.py showmigrations <app_name>`; if
it says `(no migrations)`, the migration files are missing, not just
unapplied. Fix: make sure `apps/<app_name>/migrations/*.py` (every file,
not just `__init__.py`) is actually present, then re-run `migrate`.

## Local setup - SDK (the client a customer's agent process uses)
Lives in the sibling `../resumate-sdk/` repo/folder now, not inside this one
(see "Repo layout" above) - full setup instructions are in its own README.
    cd ../resumate-sdk
    uv sync --extra dev

```python
from resumate_sdk import ResumateClient, LangGraphRunTracker

client = ResumateClient(api_key="rsm_live_...", base_url="http://localhost:8000")
tracker = LangGraphRunTracker(client, agent_name="research_agent", run_id=thread_id)

@tracker.track("web_search")
def web_search(state): ...

# ...wrap each node, build/compile your StateGraph as normal, then:
graph.invoke(initial_state)
tracker.mark_run_succeeded()
```

## Deploying (Render, database on Neon)
1. Create a free Postgres project at neon.tech - copy its connection string.
2. **Test locally first** (recommended before deploying): `export DATABASE_URL="<your Neon connection string>"`,
   then `uv run python manage.py migrate` and `uv run python -m pytest apps/ -v` -
   confirms the real database works before anything is live.
3. Push this repo to GitHub.
4. In Render: **New → Blueprint**, point it at the repo. `render.yaml` defines
   the web service (no database - that's Neon now, see step 1).
5. In Render's dashboard, set `DATABASE_URL` (Environment tab) to your Neon
   connection string by hand - it's `sync: false` in `render.yaml`
   specifically so it's never committed to git.
6. `SECRET_KEY` and `RESUMATE_ENCRYPTION_KEY` are auto-generated by Render
   (`generateValue: true` in `render.yaml`) - you don't set these by hand.
7. `build.sh` runs on every deploy: installs deps via `uv`, `collectstatic`, `migrate`.
8. Once live, real users can sign up themselves at `/accounts/signup/` -
   `createsuperuser` via Render's shell tab is only needed if you also want
   an admin account for yourself.

**Why Neon over Supabase, checked not assumed:** Neon's free tier has no
inactivity expiry and auto-resumes compute on the next query with no
manual action needed. Supabase's free tier pauses the project after a
week of inactivity and needs a manual dashboard click to resume - a real
risk for an unattended public launch, since a visitor arriving during a
paused week would hit a dead database until someone noticed. See doc.md
section 2.25.

**This started as a beta/design-partner deployment target, not a public
launch — that changed by deliberate decision** once self-serve signup
existed (see doc.md section 2.19 for the reasoning). It's now intended to
be shared publicly, free, with no subscription, for an observation
period, per that same section. See doc.md section 2.15 for what's still
deliberately deferred beyond that (Layer 3a, real vector similarity,
billing, UI redesign).

**After the first deploy:** update `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`
in `render.yaml` (or directly in Render's dashboard) from the placeholder
`resumate.onrender.com` to your actual assigned hostname — Render doesn't
know it until after the first deploy.

**Actually verified this session, not assumed** (an earlier version of
this section claimed `render.yaml`/`build.sh` existed and were verified
when they did not — see doc.md section 2.24 for the full correction):
`build.sh` run end-to-end (`collectstatic` + `migrate` both clean under
`DEBUG=False`), the app run under real `gunicorn` (added as an actual
dependency - it wasn't one before) and hit with real HTTP requests
including the `X-Forwarded-Proto: https` header Render's proxy sends,
returning genuine `200`s. **The database switch itself was verified
against a real, locally-installed Postgres 16 - not just read from
`settings.py`: migrations applied clean, and the full test suite passed
74/74 against it**, with no SQLite/Postgres divergence bugs found.

## Running tests
    uv sync --extra dev          # see "Known issue" above re: plain pip
    python -m pytest apps/ -v

74 tests across accounts (including self-serve signup + rate limiting),
integrations (checkpoint API), workflows (Layer 1
idempotency), repairs (Layer 2 + case library + failure classification/dead-lettering
+ agent-ledger receipt handling), and dashboard (auth/org-scoping,
including `LogoutCacheTests` - see doc.md section 2.23).
Also runs on every push via `.github/workflows/tests.yml`. See
`../resumate-sdk/README.md` for the SDK's own separate test suite (now a
sibling repo, not a subfolder of this one).

## Status (Steps 1-16 of the MVP plan)
- [x] Accounts app: Organization, custom User, APIKey (`apps/accounts`)
- [x] Integrations app: Connection model + `BaseOrchestratorAdapter` interface,
      `LangGraphAdapter` (push-model) implemented, CrewAI/n8n adapters stubbed
      (`apps/integrations`)
- [x] Layer 1: Workflow/Run/StepCheckpoint/ResumeInstruction models +
      `record_step_event` service, WITH VERIFIED idempotency (no duplicate
      side effects on replayed step events) and support for multiple
      sequential repair attempts per run (`apps/workflows`)
- [x] Checkpoint API: `POST /api/v1/checkpoints/`, `GET/POST .../resume/`
      (`apps/integrations/api_views.py`), Bearer-token auth via `APIKey`
- [x] `resumate-sdk`: real, separately-installable package (now its own
      sibling repo, `../resumate-sdk/`, not nested in this one - own
      `pyproject.toml`, `uv`-managed, public and `pip install git+...`-able
      so design partners aren't given access to this private backend repo
      just to get the client) - `ResumateClient`
      (HTTP, real retry/backoff/fail-open handling) + `LangGraphRunTracker`
      (node-wrapping decorator). Verified against a **real**
      `langgraph.StateGraph` and a **real** running Django server.
- [x] SDK hardening: real retry/backoff (exponential + jitter, honors
      `Retry-After`), fail-open by default (a Resumate outage no longer
      crashes a customer's agent run on a *successful* step), and a proven
      guarantee that a node's own exception always wins over a reporting
      failure - even in the stricter `fail_open=False` mode
      (`resumate_sdk/client.py`, `resumate_sdk/exceptions.py`)
- [x] SDK ergonomics: `LangGraphRunTracker.resume_from_last_failure()`
      replaces manually setting `tracker._step_index` (a real rough edge
      hit during local hands-on testing). Verified end-to-end against a
      real live server and a real LangGraph run. Its docstring and
      `resumate-sdk/README.md` are explicit about a real limitation: it
      tells you where to resume from and what repaired input to use, but
      does NOT make LangGraph itself skip re-executing already-succeeded
      nodes if you re-invoke the whole graph - actually avoiding
      re-paying for completed work requires either invoking only the
      remaining node(s) yourself, or a deeper integration with
      LangGraph's own checkpointer that isn't built yet.
- [x] Layer 2: repair candidate generation + heuristic scoring (`apps/repairs`).
      No manual faking anywhere - the moment a step comes in as failed via the
      checkpoint API, `attempt_repair()` fires automatically: generates 2-3
      candidates keyed off the failure signature, picks one, persists the full
      candidate list for audit/explainability, and pushes it to the agent.
      Verified end-to-end against a real LangGraph run, including the harder
      case: a step that fails again even after a repair gets tracked as a
      *second*, independent `RepairAttempt`, with the first correctly
      auto-resolved to `outcome=failed`.
- [x] **Failure classification + dead-letter path (`apps/repairs/classification.py`)
      - directly informed by field feedback (doc.md 2.14), built ahead of the
      ledger integration on purpose since it's pure Resumate logic with no
      dependency to evaluate first.** `attempt_repair()` now classifies a
      failure BEFORE generating any candidate: `TRANSIENT` (network/timeout/
      rate-limit) and `CORRECTABLE` (malformed LLM output - JSONDecodeError/
      ValidationError/ValueError, where a corrective prompt retry has real
      value) proceed through the existing repair flow unchanged; `DETERMINISTIC`
      (TypeError/KeyError/AttributeError/PermissionError/etc. - application or
      logic bugs no retry can fix) is dead-lettered immediately, no candidate
      generated, no adapter call made, `run.status` stays `FAILED` not
      `REPAIRING`. A separate circuit breaker (`RESUMATE_MAX_REPAIR_ATTEMPTS`,
      default 3) dead-letters a step regardless of classification once it's
      failed that many times - covers a "transient"-classified error that
      turns out to be a dependency that's actually permanently down.
      **A real design tension was resolved explicitly, not glossed over:**
      the existing heuristics already treated JSONDecodeError/ValueError/
      ValidationError as retryable; community feedback warned against
      auto-retrying "schema failures" — resolved by splitting CORRECTABLE
      (malformed output, worth a *modified* retry) from DETERMINISTIC (code
      bugs, no retry helps) rather than lumping both under one "risky" bucket.
      **A real visibility gap was found and fixed while building this:**
      Run Detail previously only rendered `resume_instructions`, which a
      dead-lettered attempt never has (nothing was proposed to resume) -
      would have made dead-lettering invisible in the UI, defeating its
      purpose. Now renders actual `repair_attempts` instead, including a
      distinct "Dead-lettered (needs human review)" badge on both Run Detail
      and the Repairs page. Verified through the real HTTP checkpoint API
      (not just the service function): a `KeyError` sent through
      `POST /api/v1/checkpoints/` correctly returns `run_status: "failed"`
      with `repaired_input: null`, and both dashboard pages render the
      dead-lettered attempt and its rationale correctly.
- [x] **`agent-ledger` integration (`apps/workflows/models.py`'s
      `side_effect_receipt` field, `apps/repairs/services.py`,
      `resumate-sdk/resumate_sdk/ledger.py`) - evaluated a real, installed
      copy of `agent-ledger` (not just its docs) before designing anything
      around it.** Confirmed live: its handler executes exactly once
      across two identical calls, and `find_by_idem_key()` gives a
      receipt-lookup with no execution side effect - exactly what "check
      its receipt" needs.
      **A real architecture question this surfaced, not glossed over:**
      `agent-ledger`'s `run()` wraps actual execution, which can only ever
      happen in the customer's own process - the Django server never
      executes a customer's tools. So the real protection against a
      duplicate side effect necessarily lives client-side
      (`resumate-sdk/resumate_sdk/ledger.py`'s `run_idempotent()`, a thin
      wrapper around the real `agent-ledger` API), and the server-side
      half in `attempt_repair()` can only *consult* a receipt the client
      already produced, not create the protection itself. `report_step()`
      gained an optional `side_effect_receipt` param; `track()`'s
      failure-reporting path picks one up via `getattr(exc,
      "side_effect_receipt", None)` - a duck-typed convention, not a
      required exception subclass - so customer code can attach a receipt
      to any exception before raising it.
      Server-side, `attempt_repair()` now checks the receipt FIRST,
      before classification or the case library: a receipt confirming the
      side effect already `succeeded` resolves the step immediately
      (`outcome=SUCCEEDED`, strategy `resume_with_confirmed_receipt`) and
      never generates a retry candidate - overriding even a normally-dead-
      lettered `DETERMINISTIC` classification, since the receipt is ground
      truth regardless of what error type got reported alongside it. A
      `failed` receipt confirms a normal retry is safe and annotates the
      rationale; no receipt at all on an apparent side-effecting step
      (`step_name` starting with `"Tool:"`) adds an explicit caution note
      to the audit trail without changing what gets retried.
      **Verified with the actual failure mode this exists to prevent, not
      just unit tests:** a real `LangGraphRunTracker` node executes a real
      `agent-ledger`-wrapped "Stripe charge," then something after the
      charge crashes; the receipt travels to a live Django server over
      real HTTP; `attempt_repair()` correctly resolves via the confirmed
      receipt instead of proposing a retry; the client resumes using the
      confirmed output per the `resumate_confirmed`/`confirmed_output`
      convention - and the "Stripe" call is confirmed to have fired
      **exactly once** across the entire failure-and-resume cycle.
- [x] **Real LangGraph-native resume (`resumate-sdk/resumate_sdk/langgraph.py`'s
      `resume_graph_native()`) - closes the gap flagged all the way back in
      Step 4.** Verified directly against a real installed LangGraph before
      any SDK code was written: a 3-node graph compiled with `MemorySaver`,
      node 3 failing, resumed via `invoke(None, config)` - nodes 1-2
      executed exactly once total across the whole cycle, only node 3
      (the failed one) executed twice. The companion mechanism -
      injecting a repair's parameters via `graph.update_state()` before
      resuming - verified the same way: a retried node genuinely received
      the injected value, not the original one.
      Since Resumate has no visibility into an arbitrary customer's LangGraph
      `State` schema, the ledger-confirmed-receipt case (previous entry)
      **raises `ValueError`** rather than guessing how to map
      `confirmed_output` onto unknown fields - an explicit, actionable
      error beats silently injecting a wrong key into someone's graph
      state. Verified end-to-end, no manual faking: the same 3-node demo
      agent used throughout this project, run against a **live Django
      server**, with the server's Layer 2 engine auto-generating a real
      repair for a real failure, then `resume_graph_native()` applying it
      through LangGraph's own checkpointer - confirmed via an actual call
      counter that the two earlier nodes ran exactly once total across the
      entire failure-and-resume cycle. The "don't re-pay for completed
      work" claim now fully holds, not just partially as it did before.
- [x] **Layer 3.5: case library (`apps/repairs/case_library.py`, `Case` model)
      - the actual moat.** Every resolved `RepairAttempt` (succeeded or
      failed) becomes a `Case`. `attempt_repair()` now checks the case
      library FIRST, before falling back to the static heuristic:
      - **Verified with a real end-to-end test:** run 1 hits a never-before-
        seen `TimeoutError` on `Tool: fetch_page` -> falls back to the
        static heuristic (`retry_with_longer_timeout`), the retry succeeds,
        a `Case` gets written. Run 2 - a **completely separate agent run** -
        hits the exact same failure signature -> correctly retrieves and
        reuses the historically successful repair from the case library
        instead of re-deriving it from heuristics, with the rationale
        `"Case library: 1/1 past successes for this exact failure signature"`
        recorded in the audit trail.
      - **Honest scope limit, stated in code comments too**
        (`case_library.py`'s module docstring): this is **exact
        failure_signature string matching only**, not the embedding-based
        similarity search doc.md describes for this layer. Real vector
        similarity needs Postgres+pgvector in production (dev here runs on
        sqlite) and a real embedding API call (no credentials available in
        this environment). `find_best_known_strategy()` is written as the
        single seam every caller goes through specifically so swapping in
        real vector similarity later doesn't touch `attempt_repair()` or
        anything else.
- [x] **Auth + org-scoping (`apps/accounts/urls.py`, `apps/dashboard/views.py`) -
      fixes a real cross-tenant data leak.** Every dashboard view previously
      showed ALL organizations' runs/repairs mixed together with no login
      required at all. Now: login/logout via Django's built-in auth views
      against the custom User model, every view requires authentication, and
      every query is scoped to `request.user.organization` via a shared
      `OrgScopedView` base class. Verified with two separate orgs and real
      data for each:
      - Unauthenticated access to `/runs/` correctly redirects to login
      - Org A's user sees Org A's runs, never Org B's, on the list view
      - **The harder case:** Org A's user directly guessing Org B's run URL
        gets "Run not found," not Org B's actual data
      - Dashboard KPI cards now compute real numbers (total runs, failure
        rate, auto-repair count, repair success rate) from each org's own
        data instead of the original mock `1842`/etc - confirmed the old
        mock number is gone from a real logged-in response
      - No self-serve signup yet - accounts are provisioned via Django admin,
        stated explicitly on the login page itself
      - `$ saved` / `avg time saved` KPIs and the two trend charts are left
        empty/illustrative rather than showing fake numbers next to now-real
        ones - they need a cost-per-call assumption and day-bucketed
        aggregation respectively, neither built yet
- [x] Dashboard Runs, Run Detail, and Repairs pages wired to real data (now
      org-scoped), Repairs page also shows case-library stats (total cases,
      unique failure signatures seen)
- [x] **Onboarding: API key self-service UI + Integrations page
      (`apps/dashboard/views.py`, `templates/dashboard/integrations.html`) -
      closes the biggest onboarding blocker.** Previously the only way to
      get an API key or see connection status was for someone to run
      `manage.py shell`. Now, org-scoped throughout:
      - Visiting `/integrations/` eagerly provisions the org's LangGraph
        `Connection` (idempotent, safe to call repeatedly) so there's always
        something concrete to show, and shows real status + last-event time
      - Create an API key via a form; the raw secret is shown exactly once
        via the POST/redirect/GET pattern (stashed in the session, popped
        and discarded on the very next page load) - verified by capturing
        the actual raw secret from a real response and confirming it never
        reappears on a subsequent load, while the short identifying prefix
        (`rsm_live_...`) correctly continues to show
      - Revoke a key; verified the harder case too - a user from a
        **different org** guessing another org's key ID gets a silent
        no-op, not an error that would confirm the key even exists
      - Setup instructions (pip install + code snippet) auto-populate with
        the real key immediately after creation, falling back to a
        placeholder otherwise
- [x] **Automated test suite (`apps/*/tests.py`, `resumate-sdk/tests/`,
      `.github/workflows/tests.yml`) - 30 Django tests + 5 SDK tests,
      running in CI on every push.** Every guarantee manually verified
      throughout earlier development is now codified as a repeatable test:
      Layer 1 idempotency, the `ResumeInstruction` multi-attempt regression,
      Layer 2's auto-trigger through the real HTTP endpoint, the
      second-failure/second-attempt case, case-library learning behavior
      AND its org-scoping (a gap only caught while writing this - not
      manually tested before), full cross-tenant isolation including
      direct-URL-guessing, the one-time API key secret pattern, and the
      SDK's fail-open/exception-masking guarantees. **Proven to actually
      catch regressions, not just pass:** the `ResumeInstruction` OneToOne
      bug was deliberately reintroduced after the suite was written
      specifically to check whether it would be caught - it was, failing 2
      of 5 Layer 1 tests immediately, then the fix was reverted and the
      full suite reconfirmed green.
- [x] **Production deployment config (Render): `render.yaml`, `build.sh`,
      `config/settings.py`'s production hardening - actually built and
      verified this session; an earlier version of this bullet described
      these as already done when `render.yaml`/`build.sh` didn't exist
      and `gunicorn` wasn't even a dependency (see doc.md section 2.24).**
      `DEBUG=False` enables `SECURE_SSL_REDIRECT`, HSTS, and secure cookies
      automatically; whitenoise serves static files without needing
      a separate nginx; Redis is optional (falls back to local-memory
      cache - no paid addon required just to deploy, since the task queue
      already runs on Postgres via django-tasks-db, not Redis); Postgres
      SSL mode handled automatically when `DATABASE_URL` is postgres - and
      this session actually installed a real local Postgres 16 and
      confirmed it, migrations clean, full suite 74/74 passing against it.
      **This was originally a beta/design-partner deployment target, not
      a public launch** - see the self-serve signup entry below for the
      deliberate pivot away from that plan. Verified for real this
      session: a genuinely fresh venv install (fixed a real
      `pyproject.toml` packaging bug in the process - see "Known issue"
      above), `build.sh` run end-to-end (`collectstatic` + `migrate` both
      clean under `DEBUG=False`), and the app actually served under real
      **gunicorn**, hit with HTTP requests including the
      `X-Forwarded-Proto: https` header Render's proxy sends, returning
      genuine `200`s on the landing and login pages.


- [ ] Layer 3a: contextual bandit learning loop (`apps/learning`) - not started.
      Distinct from Layer 3.5: the case library answers "how has this exact
      failure been resolved before"; the bandit is meant to handle
      exploration/exploitation tradeoffs and structural workflow rewrites
      over time, per doc.md section 3.
- [ ] Real vector similarity for the case library (see scope note above) -
      needs a Postgres+pgvector deployment and an embedding API budget,
      neither available in this dev environment.
- [x] **Self-serve signup (`apps/accounts/forms.py`, `views.py`,
      `rate_limit.py`) - a deliberate strategic pivot, not scope creep.**
      Public, free, no-subscription launch changes admin-provisioning from
      "correctly cautious" to "cannot scale to strangers" - built to match
      that plan change, not built ahead of it. `SignupForm` creates an
      `Organization` + its first `User` (as owner) together, with unique
      slug generation on collision. Since public+free+no-subscription
      removes the natural abuse limiter admin-provisioning provided,
      shipped alongside it: a dependency-free IP-based rate limiter
      (`apps/accounts/rate_limit.py`, Django's existing cache framework)
      and a CSS-hidden (not `type="hidden"` - some bots skip those)
      honeypot field. Neither is a complete abuse-prevention story (no
      email verification, no CAPTCHA) - both are honestly scoped as
      "good enough for a beta-scale public launch," with stronger
      protection flagged as a fast-follow if real abuse shows up, not
      built preemptively.
      **A real test-isolation bug surfaced by running the suite, not
      anticipated:** the rate limiter's cache doesn't reset between test
      methods the way the DB does - earlier tests exhausting the real
      rate limit against the same test-client IP caused unrelated later
      tests to fail with confusing errors, fixed by clearing the cache in
      `setUp()`. Verified beyond the automated suite: a real signup page
      load and submission through a live server, confirming the honeypot
      is genuinely hidden in the actual rendered HTML, a successful
      signup lands on the dashboard (not login), and a simulated bot
      filling the honeypot is silently rejected. 13 new tests, 56 Django
      tests total, all passing.
- [x] **The abuse-protection gap this exposed, closed properly (`apps/core/rate_limit.py`,
      `apps/integrations/api_views.py`, `apps/dashboard/views.py`'s
      `CreateAPIKeyView`).** Signup's rate limiter only ever protected
      the signup form itself - two real gaps remained open even after
      that shipped: unlimited API keys per org, and zero rate limiting
      on the checkpoint API (the endpoint that does actual DB writes and
      triggers real repair-engine work on every hit, arguably the more
      important one to protect). Both closed now:
      - **API key cap** (`RESUMATE_MAX_API_KEYS_PER_ORG`, default 10):
        only ACTIVE keys count - a revoked key frees its slot. Verified
        per-org, not global (Org A hitting its cap never blocks Org B).
      - **Checkpoint API rate limit** (`RESUMATE_CHECKPOINT_API_RATE_LIMIT_PER_MINUTE`,
        default 300/min), applied to all three checkpoint-API endpoints,
        **keyed by API key, not IP** - a legitimate customer's agent may
        run from a shared/rotating cloud IP, which would make IP-based
        limiting either too strict for real users or trivially bypassed
        by an attacker. Verified per-key, not shared - one key hitting
        its limit never blocks a different key, and a failed auth
        attempt never consumes a valid key's budget.
      **`rate_limit.py` was moved from `apps.accounts` to `apps.core`
      while building this** - it started accounts-specific for signup,
      but `apps.integrations` needing it too made that an awkward
      cross-app dependency; `apps.core` is the correct shared home.
      **A real missing piece found and fixed along the way:** the API
      key cap's rejection uses Django's messages framework, which had
      never actually been wired up to render anywhere - `base.html` had
      no messages block at all until this. Fixed once, now available
      for any future feature that needs to show a one-off message.
      Verified end-to-end on a live server: the real dashboard page
      shows the cap-exceeded message, the key count stays capped, and
      real sequential HTTP requests against the checkpoint API show the
      exact expected 200/200/200/429/429 pattern at a limit of 3.
      6 new tests, 62 Django tests total, all passing.
- [x] **Dashboard's real range filter + trend charts, and a functional Settings page
      (`apps/dashboard/views.py`, `templates/dashboard/index.html`,
      `templates/dashboard/settings.html`).** Two real, confirmable gaps
      found by actually reading the existing code rather than assuming
      it worked: (1) the 24h/7d/30d buttons only changed which button
      was *highlighted* - the underlying queries had no date filter at
      all, so every KPI silently showed all-time data no matter which
      was selected; (2) `failure_trend`/`repair_success_trend` were
      hardcoded to empty lists, honestly commented as "not built yet"
      but still non-functional. Settings was a pure placeholder with no
      view logic at all.
      **Fixed:** `runs`/`repairs` querysets are now genuinely filtered
      by `started_at`/`created_at` against the selected window; both
      trend charts are real day-bucketed aggregations
      (`TruncDate` + `Count`/`Q` for failure rate and repair success
      rate) over that same window, with an honest "No runs in this
      period yet" message for a brand-new org rather than a silently
      blank chart - which now matters directly, since self-serve signup
      means new orgs land here with genuinely zero data.
      **Settings** now shows real organization info (name, plan, member-
      since date, the viewer's own role) and a working name-edit form -
      gated by `User.role`, which had existed on the model since the
      very first commit but had never actually been enforced anywhere
      until this: only an OWNER/ADMIN sees the edit form or can submit
      it; a MEMBER sees the same info read-only, and a POST from a
      member is rejected server-side, not just hidden client-side.
      Verified end-to-end on a live server, not just the test suite:
      three runs seeded at 0/10/45 days old, confirming 24h shows only
      today's run, 7d adds the 10-day-old one but still excludes the
      45-day-old one, and 30d includes both recent ones while still
      excluding the 45-day-old one; a real settings form submission
      confirmed to actually update the database and show a success
      message. 12 new tests, 71 Django tests total, all passing.
- [x] **Public landing page at root (`apps/landing`, `config/urls.py`) -
      closes the "no public page" gap for the self-serve, publicly-shared
      beta.** `/` previously redirected any logged-out visitor straight
      to `/accounts/login/` via `OrgScopedView`'s `LoginRequiredMixin` -
      there was no public-facing page at all. Fixed with a new, separate
      `apps/landing` app owning `/`: an anonymous visitor sees a new
      product landing page (hero, problem framing, the three-layer
      checkpoint/repair/learn explainer, Log in / Sign up buttons); an
      authenticated visitor is redirected straight to the dashboard,
      which moved from `/` to `/app/` to make room. Every dashboard view
      still requires login exactly as before, completely unchanged.
      **A real regression this caused was found and fixed by actually
      running the suite, not by reading the diff:** 20 of the then-71
      tests hardcoded pre-`/app/` paths and started 404ing; all were
      updated to the new routing, full suite passing again. See doc.md
      sections 2.22-2.23 for the full account, including an earlier,
      different, never-actually-applied draft of this same feature that
      section 2.22 now corrects.
- [x] **Logout + browser back-button fix (`apps/dashboard/views.py`) -
      pressing Back after logout no longer shows the stale dashboard.**
      Logout itself was already correct (session genuinely destroyed);
      the browser was restoring the dashboard from its own back/forward
      cache without asking the server again. `Cache-Control: no-store`
      (via `never_cache` on `OrgScopedView`) fixes it - verified with an
      isolated repro before touching the real file, then confirmed
      against the real repo: 74/74 tests passing, including 3 new
      regression tests. See doc.md section 2.23.
- [ ] `Connection.get_or_create_default`'s one-connection-per-org-per-orchestrator
      simplification (see its docstring) will need revisiting once multi-connection
      support matters
- [ ] UI is still the original Tailwind/CDN scaffold from project kickoff -
      every change since has been wiring real data into the existing layout,
      not a visual redesign. Deliberately deferred until the mechanism (Layers
      1-3.5) was proven, per earlier discussion - worth revisiting now that
      it is.