#!/usr/bin/env python3
"""
Analyze BLIS native cohort-based workload trace against spec.
Validates arrival rates, gamma arrival process, and token distributions per cohort.
"""

import csv
import yaml
import math
import sys
from collections import defaultdict
import numpy as np
from scipy import stats

def compute_gamma_metrics(shape, trace_rate):
    """
    Compute theoretical gamma distribution metrics from shape and trace_rate.

    CRITICAL: BLIS ignores cv and scale fields in spec!
    Actual behavior:
      - Reads: shape and spike.trace_rate
      - Calculates: scale = (1/trace_rate) / shape
      - Generates: gamma(shape, calculated_scale) arrivals
    """
    expected_mean_iat = 1.0 / trace_rate  # seconds
    calculated_scale = expected_mean_iat / shape
    theoretical_cv = 1 / math.sqrt(shape)
    theoretical_skew = 2 / math.sqrt(shape)

    return {
        'expected_mean_iat': expected_mean_iat,
        'calculated_scale': calculated_scale,
        'theoretical_cv': theoretical_cv,
        'theoretical_skew': theoretical_skew
    }

def compute_lognormal_metrics(mu, sigma):
    """Compute expected mean and std for lognormal distribution."""
    expected_mean = math.exp(mu + sigma**2 / 2)
    expected_std = math.sqrt((math.exp(sigma**2) - 1) * math.exp(2*mu + sigma**2))
    return expected_mean, expected_std

def analyze_cohort(cohort_id, cohort_spec, trace_rows):
    """Analyze a single cohort against its spec."""
    print(f"\n{'='*80}")
    print(f"COHORT: {cohort_id}")
    print(f"{'='*80}")

    results = {}

    # 1. Arrival Rate
    print(f"\n1a. Arrival Rate")
    expected_rate = cohort_spec['spike']['trace_rate']
    duration_us = cohort_spec['spike']['duration_us']
    expected_count = int(expected_rate * duration_us / 1e6)

    arrivals = sorted([int(r['arrival_time_us']) for r in trace_rows])
    actual_count = len(arrivals)

    if len(arrivals) > 1:
        actual_duration_s = (arrivals[-1] - arrivals[0]) / 1e6
        actual_rate = actual_count / actual_duration_s
    else:
        actual_rate = 0
        actual_duration_s = 0

    rate_diff_pct = abs(actual_rate - expected_rate) / expected_rate * 100 if expected_rate > 0 else 0
    count_diff_pct = abs(actual_count - expected_count) / expected_count * 100 if expected_count > 0 else 0

    rate_verdict = "PASS" if rate_diff_pct < 5 else "FAIL"
    count_verdict = "PASS" if count_diff_pct < 2 else "FAIL"

    print(f"Expected: {expected_rate:.2f} req/s × {duration_us/1e6:.0f}s = {expected_count} requests")
    print(f"Actual: {actual_rate:.2f} req/s over {actual_duration_s:.1f}s = {actual_count} requests")
    print(f"Rate diff: {rate_diff_pct:.1f}% → {rate_verdict}")
    print(f"Count diff: {count_diff_pct:.1f}% → {count_verdict}")

    results['rate_verdict'] = rate_verdict
    results['count_verdict'] = count_verdict

    # 2. Arrival Process (Gamma)
    print(f"\n1b. Arrival Process (Gamma Distribution)")

    spec_shape = cohort_spec['arrival']['shape']
    spec_cv = cohort_spec['arrival']['cv']
    spec_scale = cohort_spec['arrival']['scale']

    # Compute what BLIS actually uses
    gamma_metrics = compute_gamma_metrics(spec_shape, expected_rate)

    print(f"⚠️  Spec cv={spec_cv:.4f}, scale={spec_scale:.2f} are METADATA ONLY (ignored by BLIS)")
    print(f"✓ BLIS uses: shape={spec_shape:.4f}, trace_rate={expected_rate:.2f}")
    print(f"✓ BLIS calculates: scale = 1/rate/shape = {gamma_metrics['calculated_scale']:.6f}")

    if len(arrivals) > 1:
        iats = np.diff(arrivals) / 1e6  # Convert to seconds
        actual_mean_iat = np.mean(iats)
        actual_cv = np.std(iats) / actual_mean_iat if actual_mean_iat > 0 else 0
        actual_skew = stats.skew(iats)

        mean_iat_diff_pct = abs(actual_mean_iat - gamma_metrics['expected_mean_iat']) / gamma_metrics['expected_mean_iat'] * 100
        cv_diff_pct = abs(actual_cv - gamma_metrics['theoretical_cv']) / gamma_metrics['theoretical_cv'] * 100
        skew_diff_pct = abs(actual_skew - gamma_metrics['theoretical_skew']) / gamma_metrics['theoretical_skew'] * 100

        mean_verdict = "PASS" if mean_iat_diff_pct < 5 else "FAIL"
        cv_verdict = "PASS" if cv_diff_pct < 10 else "FAIL"
        skew_verdict = "PASS" if skew_diff_pct < 20 else "FAIL"

        print(f"\nValidation (against shape + trace_rate):")
        print(f"  Mean IAT: expected={gamma_metrics['expected_mean_iat']:.6f}s, actual={actual_mean_iat:.6f}s, diff={mean_iat_diff_pct:.1f}% → {mean_verdict}")
        print(f"  CV: theoretical={gamma_metrics['theoretical_cv']:.4f}, actual={actual_cv:.4f}, diff={cv_diff_pct:.1f}% → {cv_verdict}")
        print(f"  Skewness: theoretical={gamma_metrics['theoretical_skew']:.4f}, actual={actual_skew:.4f}, diff={skew_diff_pct:.1f}% → {skew_verdict}")

        results['arrival_verdict'] = "PASS" if mean_verdict == "PASS" and cv_verdict == "PASS" else "FAIL"
    else:
        print("Not enough data points for IAT analysis")
        results['arrival_verdict'] = "SKIP"

    # 3. Input Token Distribution
    print(f"\n1c. Input Token Distribution (Lognormal)")
    mu = cohort_spec['input_distribution']['params']['mu']
    sigma = cohort_spec['input_distribution']['params']['sigma']
    expected_mean, expected_std = compute_lognormal_metrics(mu, sigma)

    input_tokens = [int(r['input_tokens']) for r in trace_rows]
    actual_mean = np.mean(input_tokens)
    actual_std = np.std(input_tokens)

    mean_diff_pct = abs(actual_mean - expected_mean) / expected_mean * 100
    std_diff_pct = abs(actual_std - expected_std) / expected_std * 100

    mean_verdict = "PASS" if mean_diff_pct < 10 else "FAIL"
    std_verdict = "PASS" if std_diff_pct < 20 else "FAIL"

    print(f"Expected: mean={expected_mean:.1f}, std={expected_std:.1f}")
    print(f"Actual: mean={actual_mean:.1f}, std={actual_std:.1f}")
    print(f"Mean diff: {mean_diff_pct:.1f}% → {mean_verdict}")
    print(f"Std diff: {std_diff_pct:.1f}% → {std_verdict}")

    results['input_verdict'] = "PASS" if mean_verdict == "PASS" and std_verdict == "PASS" else "FAIL"

    # 4. Output Token Distribution
    print(f"\n1d. Output Token Distribution (Lognormal)")
    mu = cohort_spec['output_distribution']['params']['mu']
    sigma = cohort_spec['output_distribution']['params']['sigma']
    expected_mean, expected_std = compute_lognormal_metrics(mu, sigma)

    output_tokens = [int(r['output_tokens']) for r in trace_rows]
    actual_mean = np.mean(output_tokens)
    actual_std = np.std(output_tokens)

    mean_diff_pct = abs(actual_mean - expected_mean) / expected_mean * 100
    std_diff_pct = abs(actual_std - expected_std) / expected_std * 100

    mean_verdict = "PASS" if mean_diff_pct < 10 else "FAIL"
    std_verdict = "PASS" if std_diff_pct < 20 else "FAIL"

    print(f"Expected: mean={expected_mean:.1f}, std={expected_std:.1f}")
    print(f"Actual: mean={actual_mean:.1f}, std={actual_std:.1f}")
    print(f"Mean diff: {mean_diff_pct:.1f}% → {mean_verdict}")
    print(f"Std diff: {std_diff_pct:.1f}% → {std_verdict}")

    results['output_verdict'] = "PASS" if mean_verdict == "PASS" and std_verdict == "PASS" else "FAIL"

    # 5. SLO Class & Priority
    print(f"\n1e. SLO Class & Priority")
    slo_class = cohort_spec['slo_class']
    priorities = set([r['vllm_priority'] for r in trace_rows if r['vllm_priority']])

    print(f"SLO class: {slo_class}")
    print(f"Priority values in trace: {priorities}")

    if len(priorities) == 1:
        results['priority_verdict'] = "PASS"
        print(f"→ PASS (consistent priority)")
    elif len(priorities) == 0:
        results['priority_verdict'] = "WARN"
        print(f"→ WARN (no priority values)")
    else:
        results['priority_verdict'] = "FAIL"
        print(f"→ FAIL (inconsistent priority values)")

    return results

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <workload.yaml> <data.csv>")
        sys.exit(1)

    workload_file = sys.argv[1]
    trace_file = sys.argv[2]

    # Load workload spec
    with open(workload_file) as f:
        workload = yaml.safe_load(f)

    # Load trace
    with open(trace_file) as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    print(f"Loaded {len(all_rows)} requests from trace")

    # Group by cohort (using client_id prefix)
    cohort_rows = defaultdict(list)
    for row in all_rows:
        # Extract cohort ID from client_id (e.g., "morning-standard-0" -> "morning-standard")
        client_id = row['client_id']
        cohort_id = '-'.join(client_id.split('-')[:-1]) if client_id else 'unknown'
        cohort_rows[cohort_id].append(row)

    # Analyze each cohort
    all_results = {}
    for cohort_spec in workload['cohorts']:
        cohort_id = cohort_spec['id']
        if cohort_id in cohort_rows:
            results = analyze_cohort(cohort_id, cohort_spec, cohort_rows[cohort_id])
            all_results[cohort_id] = results
        else:
            print(f"\n⚠️  Cohort {cohort_id} not found in trace!")

    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")

    total_pass = 0
    total_fail = 0
    total_warn = 0

    for cohort_id, results in all_results.items():
        print(f"\n{cohort_id}:")
        for check, verdict in results.items():
            print(f"  {check}: {verdict}")
            if verdict == "PASS":
                total_pass += 1
            elif verdict == "FAIL":
                total_fail += 1
            elif verdict == "WARN":
                total_warn += 1

    print(f"\nOverall: PASS={total_pass}, FAIL={total_fail}, WARN={total_warn}")

if __name__ == '__main__':
    main()
