# Clinical Trial Registry Change Monitor

Ingests every fetched version of clinicaltrials.gov trial registrations and
detects post-enrolment outcome changes. v0.1 status: ingestion + extraction +
normalisation only (see `docs/superpowers/plans/2026-08-08-trial-registry-monitor.md`
for the full roadmap).

## Ingestion notes

**P3 era check (Task 2):** `NCT00000620` (ACCORD, started 1999-09) was fetched
via the `int` history API and confirmed to serve the same modern JSON schema
(`protocolSection.{identificationModule,statusModule,outcomesModule,...}`) as
a 2020-registered trial like `NCT04280705`. Era differences observed, handled
defensively in `ctcm/extract.py`:
- `startDateStruct` on early versions can be missing the `type` (ACTUAL/ESTIMATED)
  key entirely (no such flag existed at registration time).
- `outcomesModule` can be `{}` (no `primaryOutcomes`/`secondaryOutcomes`) on v0
  for trials registered before outcomes were a required field.
- `primaryCompletionDateStruct` may be absent on early versions.
- Outcome `description` text may contain raw HTML (`<p>...</p>`) on some
  versions; not stripped since only `measure`/`timeFrame` feed matching.

Conclusion: no separate legacy parser needed; one extraction path handles both
eras as long as every field access defaults to `None`/`[]` instead of raising.
