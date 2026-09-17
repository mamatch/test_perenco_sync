## Assumptions

1. **Archiving order vs. same-run candidates.** Archive candidates are resolved deepest-first so a platform and its only section can be archived together in the same run, an edge case found by testing (`OLW-OLD`/`OLW-OLD_PROD`) rather than anticipated ([`canonical.py::has_active_descendant()`](pipeline/pipeline/canonical.py#L56)).
2. **"Disappeared" reconciliation matches by `code` alone**, correct for this single-tenant sandbox: it decommissions two stale `GLOBAL`-sourced rows and lets the CMMS overwrite a live one's criticality, since CMMS owns that attribute.
3. **An unrecognised criticality code is flagged, not fatal** — the system itself still upserts, since rejecting the whole record over a label typo would be worse than the typo ([`cmms_to_mdm.py`](pipeline/pipeline/cmms_to_mdm.py)'s `CRITICALITY_TO_ATTRIBUTE`).
4. **IoT tag resolution is two-step**, direct equipment-code match first, then a system-class shorthand fallback when exactly one such system exists under the platform — both confirmed against real historian values, not just the stated convention ([`iot.py::resolve_tag()`](pipeline/pipeline/iot.py#L95)).
5. **The historian isn't always in hours**: one tag reports in minutes, converted via a small factor table where an unrecognised unit is rejected rather than guessed, confirmed correct because the converted values land exactly on-trend with the asset's existing CMMS meter ([`iot.py::convert_to_hours()`](pipeline/pipeline/iot.py#L162)).
6. **Counter regressions are always quarantined, never auto-corrected**, holding up against two distinct real cases in the sandbox: a bad CMMS seed value and a genuine physical counter reset ([`iot_to_cmms.py::_baseline_value()`](pipeline/pipeline/iot_to_cmms.py#L146)).

## Questions still open

- Should the 10% archive-ratio threshold be computed against the *desired* MDM scope instead of the *current* active scope, and globally or per body/country?
- Is overwriting a `GLOBAL`-sourced row's `source` to `PERENCO` (#2) correct, or should multi-tenant provenance be preserved by keying on `(source, code)` instead of `code` alone?
- Does Celery in MDAdmin serve anything beyond the one legacy chain it's confirmed to run today ([`ARCHITECTURE_.md` section 2](ARCHITECTURE_.md#2-target-production-architecture)) — the one fact that would flip the orchestration recommendation toward Azure Container Apps Jobs instead?
