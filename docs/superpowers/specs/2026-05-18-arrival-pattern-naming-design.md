# Include Arrival Pattern in Campaign Directory Names

**Date:** 2026-05-18

## Problem

Campaign directories are named without the arrival pattern, making it hard to identify which pattern was used without opening files.

Current: `1-llama-3-1-8b-instruct-h100-m-mid`
Desired: `1-llama-3-1-8b-instruct-h100-m-mid-afternoon`

## Solution

Modify `make_dir_name()` in `blis-campaign/generate.py` to append arrival pattern to the directory name.

### Changes

**File:** `blis-campaign/generate.py` (lines 164-166)

```python
def make_dir_name(exp):
    """e.g. '13-qwen3-14b-h100-general-afternoon'"""
    base = f"{exp['id']}-{exp['model']}-{exp['hw']}-{exp['workload']}"
    arrival = exp.get('arrival_pattern', '')
    if arrival:
        return make_dns_name(f"{base}-{arrival}")
    return make_dns_name(base)
```

### Scope

- **Changed:** Directory naming only
- **Unchanged:** `experiment_id` (PVC paths remain stable)
- **Unchanged:** `run.py` (discovers directories via experiment.json)

### Backward Compatibility

- Uses `.get()` with fallback for missing arrival_pattern fields
- Existing directories continue to work (run.py is directory-name agnostic)
- DNS-1123 compatible via existing `make_dns_name()` sanitization
