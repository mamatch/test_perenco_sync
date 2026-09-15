# 2. Business rules

This page describes what the business expects. It deliberately does not describe *how* to detect
each case: exploring the sandbox data and asking questions during the clarification call is part of
the exercise. Write down the interpretations you settle on in your `DECISIONS.md`.

## Asset hierarchy in the CMMS

```
Body (site)                      one CMMS body per operational site of the MDM
 └─ Platform  (root asset)       platforms, living quarters…            family PLATFORM
     └─ Section                  ISO 14224 section (Production, Utilities, Safety…)   family SECTION
         └─ System               a functional system (gas compression, ESD…)        family SYS_<class>
             └─ Equipment        a maintainable item (pump, valve, generator…)        family <class>_<type>
```

Assets are identified by their **code**. The CMMS shows the code of the parent asset on each asset.
Look at the codes in the sandbox: the key users follow naming conventions, and those conventions are
what the two systems have in common.

## Data ownership

| Level | Owner (source of truth) | Consequence |
|---|---|---|
| Sites (bodies) | MDM | The connector API cannot create bodies. |
| Platforms, sections | MDM | The CMMS must reflect the MDM scope: what enters the scope is created, what leaves it is **archived** (never deleted). |
| Systems, equipments | CMMS | The MDM must reflect the CMMS, with their classification, criticality and validity. |
| Equipment types, system classes, section categories | MDM (governed reference data) | A sync never creates reference data. |
| Running hours | IoT historian | The CMMS meters must follow the historian. |

## What the business told us

* "Every platform of the MDM is a maintenance object. When a platform or a section is decommissioned
  in the MDM, maintenance must not be able to plan work on it anymore." Nothing is ever deleted in
  the CMMS, history matters.
* "Never archive something that still has active children under it; tell us instead."
* "The CMMS technicians create systems and equipments. The MDM must know them the next morning, with
  the platform and section they belong to, their class and their criticality." Criticality labels
  are typed by hand in the CMMS.
* "An equipment must have a type known to the MDM. If not, the maintenance key user has to fix the
  CMMS; do not invent reference data."
* Asset codes are chosen by the CMMS key users.
* "The MDM is the reference for names." How far you propagate renames is your call.
* "Preventive maintenance of rotating machines must be driven by running hours coming from the
  historian, daily, with one reading per machine per day in the CMMS." The CMMS meter is a
  cumulative counter in hours.

## Non-functional requirements

* Nightly batch, must finish in less than 2 hours for the production volumes: ~50 sites,
  ~1 000 platforms, ~5 000 systems, ~10 000 equipments (the sandbox is much smaller).
* CMMS API: 50 requests per minute; transient 5xx happen.
* Idempotent: running the pipeline twice in a row must not produce any write on the second run.
* Every write to the CMMS and to the MDM must be traceable to a run.
* Destructive mistakes have happened in the past (mass archiving after an empty extraction). The
  business expects the pipeline to refuse to do obviously wrong things on its own.
