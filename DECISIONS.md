# DECISIONS

Assumptions made where the statement (docs/, ARCHITECTURE_.md) was ambiguous, how the
sandbox data confirmed or contradicted them, and the questions I would still want
answered by the business. Written after building and running the pipeline against the
sandbox in `pipeline/`, so every item below is backed by an actual observation, not a
guess -- the exact command to reproduce each one is given.

## 1. MDM active-scope rule -- confirmed

ARCHITECTURE_.md flagged an apparent ambiguity: the clarification-call notes as
transcribed said `date_start < now()` and `date_end` null or `< now()`, which inverts the
usual meaning of a validity window. This was a transcription error, not the actual rule:
confirmed the intended rule is the conventional one,

    date_start <= as_of and (date_end is null or date_end > as_of)

implemented as the sole behaviour in `pipeline/timeutil.py::is_active` (no flag -- the
earlier `MDM_ACTIVE_RULE=conventional|literal` toggle was removed once this was settled,
rather than keeping an option that is now known to be wrong). Boundary cases are
unit-tested in `pipeline/tests/test_timeutil.py`.

## 2. `Asset/Filter` never returns `archived`

Confirmed by reading `mock_gmao/mock_gmao/store.py::_filter_view` and by the mock's own
test (`test_filter_mixes_archived_and_hides_flag`). The only way to know an asset's
archived flag from the connector is to call Filter once with `archived=true` and once
with `archived=false` and remember which bucket it came from -- `Asset/Get` exposes it
per-asset but calling Get for every asset does not scale to ~15 500 production assets
within the 50 req/min budget. `mdm_to_cmms._current_tree` and `cmms_to_mdm._pull_all` both
do the two-call pull; nothing in the pipeline calls Get except the IoT baseline lookup
(`iot_to_cmms._baseline_value`, called at most once per asset per run, and skipped
entirely once the audit store has a prior reading -- see run 2 in the README).

## 3. Recursive active-descendant check vs. same-run parent+child archiving

Found by testing, not by reading: `OLW-OLD` (platform) and its only section
`OLW-OLD_PROD` both leave MDM scope in the seed data (`date_end=2025-06-30` on the
platform). A first implementation checked each archive candidate against the *pre-run*
CMMS snapshot, which meant `OLW-OLD` was always blocked by its own section --
`OLW-OLD_PROD` was still "active" in that snapshot even though it was *also* about to be
archived, child-first, in the same run. Fixed by resolving candidates deepest-first and
excluding already-resolved siblings from the descendant check
(`pipeline/canonical.py::has_active_descendant`, regression-tested in
`test_platform_and_its_only_section_archive_together_in_one_run`). A genuinely unrelated
active descendant still blocks correctly (`OGD_DRIL` stays `BLOCKED`: `SYS_OGD_009` /
`OGD-CR-901` are live CMMS-owned assets under it that the MDM has no say over).

## 4. 10% archive ratio: fires for real in this sandbox

With the default 10% threshold, the very first run against the seed data blocks **every**
archive candidate (`MBK_MAR`, `MBK_UTIL`, `OLW-OLD` + its section, `RBD`): 5 eligible
archives out of 23 active platforms/sections in scope is 21.7%. This is the guardrail
doing exactly its job on a sandbox that is two orders of magnitude smaller than
production (~1 000 platforms per docs/02_business_rules.md, where the same absolute
number of stale objects would be a fraction of a percent) -- not a bug. CREATE/UPDATE
work still proceeds (6 creates + 1 update went through), matching "hard stop for
destructive actions only". To see the archives actually execute (and to prove
idempotency on them), override the threshold for a demo run:
`ARCHIVE_RATIO_THRESHOLD=0.5 uv run python -m pipeline run mdm-to-cmms`.

**Still open:** should the threshold be a percentage of the *current* active scope (what
I implemented) or of the *desired* MDM scope, and should it be computed globally or per
body/country? A single large site being fully decommissioned could still be legitimate
and would be blocked either way under a global rule.

## 5. Governed reference data: never invented, three real rejection cases found

`docs/02_business_rules.md` and ARCHITECTURE_.md section 4 both say equipment types,
system classes and section categories are MDM-governed and must never be created by the
sync. Running `cmms_to_mdm` against the untouched seed rejects exactly six assets, all
genuinely present in the data (not manufactured for the demo):

| Asset | Reason | Where in the seed |
|---|---|---|
| `OLW-P-301C` | `unknown_equipment_type` (`PU_SC`) | typed as a screw pump, no matching `EquipmentType` in `masterdata.json` |
| `OLW-P-601` | `unknown_equipment_type` (`PU`) | generic "Pump" family, no type code that specific |
| `OLW-Z-001` | `unknown_equipment_type` (`XX_ZZ`) | literally named "Mystery skid" |
| `OLW-P-ORPHAN` | `orphan_equipment` | `parentCode: null`, `bodies: []` |
| `SYS_JNR_077` | `invalid_system_parent_type` | parented directly on platform `JNR` instead of a section (named "Tchatamba **misplaced** HVAC") |
| `JNR-HV-901` | `parent_system_rejected` | cascades from the line above: its parent system was itself rejected |

These are reported (audit `dq_issues` table + `cmms_rejected` action rows), never
silently fixed or dropped, per "an equipment must have a type known to the MDM ... do not
invent reference data."

## 6. "Disappeared" reconciliation is matched by code, single-tenant

`systemref/models.py` states codes are *not* unique across tenants. This sandbox only
ever syncs one tenant (`PERENCO`), so `cmms_to_mdm` matches System/Equipment rows by
`code` alone. Two rows in the pre-loaded fixture are pre-existing, `source="GLOBAL"`
records from "a previous import" (`SYS_OLW_099`, `OLW-P-999`) that do not exist under
that code in the live CMMS at all -- the reconciliation correctly decommissions both
(`date_end` set to the run date, `disappeared_from_cmms` data-quality issue raised) since
a full reconciliation, not an upsert-only feed, was explicitly requested. `SYS_OLW_001`
*is* live in the CMMS and gets overwritten wholesale (`source` becomes `PERENCO`, `tag`
follows the CMMS name, its stale `SCE` criticality flag is replaced by `Production
critical`) -- CMMS is the owner of these attributes, so MDM-side drift never wins.

**Still open:** is overwriting a `GLOBAL`-sourced row's `source` field to `PERENCO`
correct, or should multi-tenant provenance be preserved by keying on `(source, code)`
instead of `code` alone? The single-tenant sandbox can't distinguish these; I'd confirm
with whoever owns the multi-tenant MDM import.

## 7. Criticality: unknown code flagged, not fatal

`PC` -> `Production critical`, `SCE` -> `SCE` (`cmms_to_mdm.CRITICALITY_TO_ATTRIBUTE`).
An unrecognised code (a typo, e.g. business's own example "a typo in a criticality
label") raises `unknown_criticality_code` as a data-quality issue but still lets the
system itself upsert -- rejecting the whole record over a label typo would be worse than
the typo.

## 8. IoT tag -> asset resolution: two-step, both steps confirmed against real data

`docs/04_mdm_and_iot.md` gives the convention (`country-platform.suffix.RUN_HRS`) but not
the resolution algorithm. Implemented in `pipeline/iot.py::resolve_tag`:

1. `<platform>-<suffix>` is a known, non-archived CMMS asset code -> that asset (covers
   the equipment case, e.g. `GA-OLW.K-101A` -> `OLW-K-101A`).
2. Otherwise, if exactly one asset under that platform has family `SYS_<suffix>` -> that
   system. Confirmed by data, not guessed: `GA-JNR.PG.RUN_HRS`'s values (~415 004 to
   415 020h across the export window) match **exactly** the pre-existing meter on
   `SYS_JNR_004` ("Tchatamba power generation", seed value 415 000h) -- the historian
   addresses that platform's single power-generation system by class shorthand, not by an
   individual turbine tag, because JNR has two turbines feeding one aggregate counter.
3. Otherwise: quarantined (`unresolved_tag`), never guessed. Two tags never resolve
   against this seed: `GA-OLW.P-777` (no such equipment or system class exists anywhere
   in the CMMS) and `GA-OLW.P-999` (exists as an `Equipment` in the *MDM* fixture, never
   as a CMMS asset -- consistent with item 6: that equipment also gets decommissioned by
   `cmms_to_mdm` for having disappeared from the CMMS).

## 9. Units: historian is not always in hours

`OLW-P-301B`'s tag reports in `min`, not `h` (`docs/04_mdm_and_iot.md` warns "other units
exist"). Converted via a small factor table (`pipeline/iot.py::UNIT_TO_HOURS`); an
unrecognised unit is a rejected reading (`unknown_unit:<unit>`), never a silent guess. The
converted values land exactly on-trend with the tag's pre-existing hour-based CMMS meter
(41 002h baseline -> 41 014h, 41 031h ...), which is what confirmed the conversion was
right rather than coincidentally plausible.

## 10. Counter regressions: two different real cases, both quarantined, neither fixed automatically

- `JNR-K-101`: the CMMS seed's own meter (150 000h, dated 2026-09-03) is *higher* than
  every value the historian reports for the following days (~149 916-149 983h). Every
  day is correctly quarantined against the original baseline, which never moves because
  no read is ever accepted -- this looks like a bad manual entry in the CMMS itself, and
  is exactly the kind of thing the business should be alerted to rather than have quietly
  overwritten.
- `OLW-P-301A`: a genuine counter-reset partway through the export window (98 853h drops
  to 13h, 37h, 61h ...). Same conservative handling: quarantined, baseline never moves,
  `counter_regression` data-quality issue raised every day until a human creates a new
  meter after the physical counter replacement, per the mock's own error message and
  `docs/02_business_rules.md`.

## 11. What was deliberately not built (and what a "reference" means here)

- **dbt on DuckDB**: a proof of concept for the delta-computation core was built and run
  successfully against the sandbox during development (matching `canonical.py`'s output,
  with two documented simplifications: no same-run parent+child archive co-resolution,
  and no CMMS-side meter baseline for day 1 of the IoT regression check) but was not kept
  in the final submission, to avoid a second, less-complete implementation living
  alongside the tested one.
- **Airflow**: considered and not adopted -- `ARCHITECTURE_.md` section 2 recommends
  repointing MDAdmin's existing Celery chain instead, since introducing a second "how do
  we schedule background work" mechanism isn't justified if Celery already does other jobs
  there too. See #12 below for the full reasoning and the one assumption it rests on. Not
  installed or run anywhere in this repo either way, on purpose: standing up a real
  instance (its own metadata DB, a first-run admin password, a multi-minute dependency
  install) is real operational weight for a zip that has to run unattended on an
  evaluator's machine, for a component that's graded as a design/reasoning item, not as
  something that needs to be clicked through.
- **A dashboard UI**: kept to the text health summary + SQLite audit tables
  (`pipeline/pipeline/observability.py`), as the exercise explicitly allows ("a sketch of
  the dashboard you would give to operations" -- described in `pipeline/README.md` rather
  than built, since a real one belongs in Grafana/Metabase reading the same audit tables,
  not in a one-off HTML page).
- **Cross-process rate limiting**: the CMMS client's rate limiter is per-process
  (`pipeline/pipeline/clients/cmms.py::_RateLimiter`), correct for this single-CLI
  prototype; ARCHITECTURE_.md section 2 describes the production fix (a concurrency cap on
  the CMMS-calling Celery queue) needed once several workers can call the CMMS at once.
- **Body/site reassignment**: if an existing platform's CMMS body drifted from the MDM's
  org unit, the sync reports it (name/parent are still propagated) but does not
  auto-reassign `bodyNames` on an existing asset -- moving a platform to a different site
  is an operationally heavier action than a rename, and the statement lists "orphans,
  suspicious cases" as things to report rather than silently fix.

## 12. Orchestration: repoint Celery -- topology confirmed, one question still open

`docs/01_context.md` documents exactly one Celery chain in MDAdmin -- this integration's
own five-step legacy process -- and says nothing about whether Celery is used for anything
else there. An earlier draft of `ARCHITECTURE_.md` filled that gap by *assuming* a
Kubernetes/AKS deployment (a separate broker, autoscaled worker fleet, a Beat singleton pod)
and reasoned from there. That assumption was never grounded in `docs/01_context.md` and
turned out to be wrong: confirmed on a later exchange, Celery runs on the MDAdmin/Django
instance itself, not as a separate distributed deployment. `ARCHITECTURE_.md` section 2 now
reflects that directly instead of the earlier AKS-specific reasoning (Redis-backed Beat
persistence, KEDA autoscaling, a per-replica concurrency cap), none of which applies to a
single instance.

That topology fact actually strengthens the case for repointing Celery rather than weakening
it: adding three tasks to a process that's already running is close to free either way,
whether or not Celery also does other scheduled work in MDAdmin. The remaining open question
is narrower than before -- **still open:** confirm with the MDAdmin team whether Celery is
used for anything beyond this one chain. If it turns out to serve nothing else and Perenco
would rather retire it from MDAdmin outright, **Azure Container Apps Jobs on a cron trigger**
is the alternative (`ARCHITECTURE_.md` section 2): no broker, worker or Beat process to
operate, native per-job retry, execution history through Azure Monitor, at the cost of no
native cross-task dependency graph if requirements ever grow past three independent
branches, and a "job never fired at all" failure mode that needs its own dead-man's-switch
alert (a scheduled query checking for no successful execution in the last N hours) rather
than a DAG-oriented tool surfacing a missing run more passively. Since Celery is already
running rather than something to newly provision, choosing that alternative would be a
deliberate decommissioning decision, not a technical necessity created by this integration.

## 13. MDM writes go through a Django management command, implemented for real

Confirmed during the clarification exchange: the exercise's "do not modify the sandbox
code" rule covers `systemref_lite`'s *existing* definitions (models, migrations, the other
management commands) so the mock MDM stays functional -- it does not forbid *adding* a new,
purely additive management command. `systemref_lite/systemref/management/commands/apply_sync_plan.py`
is that addition: it applies, through the Django ORM inside one `transaction.atomic()`, the
write plan `pipeline/pipeline/cmms_to_mdm.py` computes -- nothing in `systemref_lite` that
already existed before this change was touched.

The write path changed shape, not behaviour: `pipeline/pipeline/clients/mdm.py` is read-only
now (the desired-state read for MDM → CMMS, and the governed-reference/current-state reads
CMMS → MDM needs to compute its delta and its NOOP/UPDATE distinction). `cmms_to_mdm.py`
still does 100% of the validation and diffing in pure Python, exactly as before; it only
ever hands the management command entries that already passed that validation, as codes
(`system_class_code`, `platform_code`, ...), never numeric foreign keys, so the two
processes never need to agree on an ID space. The whole plan is applied as one transaction:
`clients/mdadmin.py::apply_plan()` raises `MdAdminCommandError` on any non-zero exit, and
every pending action is then recorded `FAILED_RETRYABLE` with the real stderr as the reason
-- never a partial success, since the Django side rolled back everything already (verified:
a deliberately invalid plan entry raises `SystemClass.DoesNotExist` inside the transaction,
the subprocess exits 1, and nothing from that plan -- valid entries included -- lands in
the database).

Re-run end to end from a clean sandbox reset after the change: identical action counts to
the raw-SQL version it replaced (6 rejections, 2 archived-on-disappearance, 44 updates, 4
NOOPs on the first run; 48 NOOPs and the same 6 rejections, zero new CMMS/MDM writes, on
the second), and the same criticality-overwrite and disappearance cases from `DECISIONS.md`
#5-#6 still resolve the same way, now through the ORM instead of hand-written SQL.

**Sandbox-only simplification, called out rather than hidden:** the dispatch from
`pipeline/` to the management command is a `subprocess.run(["uv", "run", "manage.py", ...])`
call, not the Celery task dispatch `ARCHITECTURE_.md` section 2 describes for production.
A subprocess call is the right stand-in for a single-machine exercise (no broker to stand
up for one write path) and demonstrates the actual boundary that matters -- writes happen
inside MDAdmin's process, through its ORM, never from outside it -- without pretending to
be a production deployment topology it isn't.
