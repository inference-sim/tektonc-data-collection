# ORC Validation Scripts

This directory contains validation scripts used by the `/blis-campaign-orc-check` skill.

Scripts are created on-demand during validation and reused/refined across
validations. They are **tracked in git** so refinements persist across clones and
teammates (the skill checks for an existing script before regenerating one).

**Current scripts:**
- `analyze_cohort_workload.py` - Per-cohort workload parity analysis
- `validate_cohort_workload.py` - Validate BLIS native cohort workload vs trace
- `analyze_runtime.py` - Runtime health / latency metrics
- `validate_exp.py` - Per-experiment trace validation (note: has a hardcoded
  example path; generalize before reuse)

These are validation tooling, NOT experiment data.
