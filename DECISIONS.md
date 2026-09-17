# DECISIONS

Assumptions made where the statement was ambiguous, each checked against real sandbox
data rather than left as a guess, and the questions I'd still want the business to answer.
One sentence each; the code/test link behind each item is where the evidence lives.

## Assumptions

1. **`Asset/Filter` never returns `archived`.** Every read pulls the CMMS tree twice (once per `archived` value) instead of calling `Asset/Get` per asset, which wouldn't scale to production volumes ([`mdm_to_cmms.py::_current_tree()`](pipeline/pipeline/mdm_to_cmms.py#L25)).
2. **Archiving order vs. same-run candidates.** Archive candidates are resolved deepest-first so a platform and its only section can be archived together in the same run, an edge case found by testing (`OLW-OLD`/`OLW-OLD_PROD`) rather than anticipated ([`canonical.py::has_active_descendant()`](pipeline/pipeline/canonical.py#L56)).
3. **The 10% archive-ratio guardrail fires for real on this seed** (21.7% vs. 10%, every archive blocked on the first run) — the guardrail doing its job on a sandbox two orders of magnitude smaller than production, not a bug ([`canonical.py::compute_plan()`](pipeline/pipeline/canonical.py#L84)).
4. **Governed reference data is never invented.** Six real seed assets are rejected (unknown equipment types, an orphan, an invalid parent) instead of having missing reference data fabricated for them ([`cmms_to_mdm.py::_reject()`](pipeline/pipeline/cmms_to_mdm.py#L281)).
5. **"Disappeared" reconciliation matches by `code` alone**, correct for this single-tenant sandbox: it decommissions two stale `GLOBAL`-sourced rows and lets the CMMS overwrite a live one's criticality, since CMMS owns that attribute.
6. **An unrecognised criticality code is flagged, not fatal** — the system itself still upserts, since rejecting the whole record over a label typo would be worse than the typo ([`cmms_to_mdm.py`](pipeline/pipeline/cmms_to_mdm.py)'s `CRITICALITY_TO_ATTRIBUTE`).
7. **IoT tag resolution is two-step**, direct equipment-code match first, then a system-class shorthand fallback when exactly one such system exists under the platform — both confirmed against real historian values, not just the stated convention ([`iot.py::resolve_tag()`](pipeline/pipeline/iot.py#L95)).
8. **The historian isn't always in hours**: one tag reports in minutes, converted via a small factor table where an unrecognised unit is rejected rather than guessed, confirmed correct because the converted values land exactly on-trend with the asset's existing CMMS meter ([`iot.py::convert_to_hours()`](pipeline/pipeline/iot.py#L162)).
9. **Counter regressions are always quarantined, never auto-corrected**, holding up against two distinct real cases in the sandbox: a bad CMMS seed value and a genuine physical counter reset ([`iot_to_cmms.py::_baseline_value()`](pipeline/pipeline/iot_to_cmms.py#L146)).
10. **Deliberately not built**: a dbt-on-DuckDB proof of concept (built, verified, then dropped to avoid a second implementation living alongside the tested one), Airflow, a dashboard UI, cross-process rate limiting, and automatic body/site reassignment — each a documented target-architecture item, not a gap discovered late.
11. **Celery already runs on the MDAdmin/Django instance itself**, not a separate Kubernetes deployment as an earlier draft wrongly assumed, which makes repointing it at three new tasks close to free regardless of whether it also serves other jobs there ([`ARCHITECTURE_.md` section 2](ARCHITECTURE_.md#2-target-production-architecture)).
12. **MDM writes go through a new, additive-only Django management command** (`apply_sync_plan`), applied in one ORM transaction, verified to roll back all-or-nothing and to reproduce identical results to the raw-SQL version it replaced ([`apply_sync_plan.py`](systemref_lite/systemref/management/commands/apply_sync_plan.py), tested in [`test_apply_sync_plan.py`](systemref_lite/systemref/tests/test_apply_sync_plan.py)).

## Questions still open

- Should the archive-ratio threshold (#3) be computed against the *desired* MDM scope instead of the *current* active scope, and globally or per body/country?
- Is overwriting a `GLOBAL`-sourced row's `source` to `PERENCO` (#5) correct, or should multi-tenant provenance be preserved by keying on `(source, code)` instead of `code` alone?
- Does Celery in MDAdmin (#11) serve anything beyond this one legacy chain — the one fact that would flip the orchestration recommendation toward Azure Container Apps Jobs instead?
