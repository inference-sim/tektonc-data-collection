#!/usr/bin/env python3
"""
Validate BLIS ORC cohort-based workload against trace data.

Checks:
1. Per-cohort arrival rate and request count
2. Gamma arrival process (mean IAT, CV, skewness)
3. Token distributions (lognormal parameters)
4. SLO class and priority mapping
"""

import csv
import yaml
import math
import sys
from collections import defaultdict
from pathlib import Path

def load_workload(workload_path):
    """Load workload specification from YAML."""
    with open(workload_path) as f:
        return yaml.safe_load(f)

def load_trace(trace_path):
    """Load trace CSV and group by cohort."""
    cohort_data = defaultdict(list)

    with open(trace_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Extract cohort ID from client_id (format: cohort-id-N)
            client_id = row['client_id']
            # Split on last hyphen to get cohort ID
            cohort_id = '-'.join(client_id.split('-')[:-1])
            cohort_data[cohort_id].append(row)

    return cohort_data

def compute_stats(values):
    """Compute mean, std, min, max of a list."""
    n = len(values)
    if n == 0:
        return 0, 0, 0, 0

    mean = sum(values) / n
    if n > 1:
        variance = sum((x - mean) ** 2 for x in values) / (n - 1)
        std = math.sqrt(variance)
    else:
        std = 0

    return mean, std, min(values), max(values)

def compute_cv_and_skewness(values):
    """Compute coefficient of variation and skewness."""
    n = len(values)
    if n < 2:
        return 0, 0

    mean, std, _, _ = compute_stats(values)
    cv = std / mean if mean > 0 else 0

    # Skewness
    if std > 0:
        m3 = sum((x - mean) ** 3 for x in values) / n
        skewness = m3 / (std ** 3)
    else:
        skewness = 0

    return cv, skewness

def validate_arrival_rate(cohort_spec, cohort_rows):
    """Validate arrival rate and request count."""
    expected_rate = cohort_spec['spike']['trace_rate']
    expected_duration_s = cohort_spec['spike']['duration_us'] / 1e6
    expected_count = int(expected_rate * expected_duration_s)

    actual_count = len(cohort_rows)

    # Compute actual rate from timestamps
    arrivals = sorted([int(r['arrival_time_us']) for r in cohort_rows])
    if len(arrivals) > 1:
        duration_s = (arrivals[-1] - arrivals[0]) / 1e6
        actual_rate = len(cohort_rows) / duration_s if duration_s > 0 else 0
    else:
        actual_rate = 0

    rate_diff = abs(actual_rate - expected_rate) / expected_rate * 100 if expected_rate > 0 else 0
    count_diff = abs(actual_count - expected_count) / expected_count * 100 if expected_count > 0 else 0

    return {
        'expected_rate': expected_rate,
        'actual_rate': actual_rate,
        'rate_diff_pct': rate_diff,
        'expected_count': expected_count,
        'actual_count': actual_count,
        'count_diff_pct': count_diff,
        'verdict': 'PASS' if rate_diff < 5 and count_diff < 5 else 'FAIL'
    }

def validate_arrival_process(cohort_spec, cohort_rows):
    """Validate gamma arrival process."""
    spec_shape = cohort_spec['arrival']['shape']
    spec_rate = cohort_spec['spike']['trace_rate']

    # Expected mean IAT from rate
    expected_mean_iat = 1.0 / spec_rate  # seconds

    # Theoretical CV and skewness from shape
    theoretical_cv = 1 / math.sqrt(spec_shape)
    theoretical_skew = 2 / math.sqrt(spec_shape)

    # Compute actual from trace
    arrivals = sorted([int(r['arrival_time_us']) for r in cohort_rows])
    iats_us = [arrivals[i+1] - arrivals[i] for i in range(len(arrivals) - 1)]
    iats_s = [iat / 1e6 for iat in iats_us]

    if len(iats_s) < 2:
        return {
            'error': 'Not enough data points',
            'verdict': 'SKIP'
        }

    actual_mean_iat, _, _, _ = compute_stats(iats_s)
    actual_cv, actual_skew = compute_cv_and_skewness(iats_s)

    mean_iat_diff = abs(actual_mean_iat - expected_mean_iat) / expected_mean_iat * 100
    cv_diff = abs(actual_cv - theoretical_cv) / theoretical_cv * 100
    skew_diff = abs(actual_skew - theoretical_skew) / theoretical_skew * 100

    verdict = 'PASS' if (mean_iat_diff < 5 and cv_diff < 10 and skew_diff < 20) else 'WARN' if cv_diff < 20 else 'FAIL'

    return {
        'expected_mean_iat': expected_mean_iat,
        'actual_mean_iat': actual_mean_iat,
        'mean_iat_diff_pct': mean_iat_diff,
        'theoretical_cv': theoretical_cv,
        'actual_cv': actual_cv,
        'cv_diff_pct': cv_diff,
        'theoretical_skew': theoretical_skew,
        'actual_skew': actual_skew,
        'skew_diff_pct': skew_diff,
        'verdict': verdict
    }

def validate_token_distribution(cohort_spec, cohort_rows, dist_type='input'):
    """Validate lognormal token distribution."""
    if dist_type == 'input':
        dist_spec = cohort_spec['input_distribution']
        token_field = 'input_tokens'
    else:
        dist_spec = cohort_spec['output_distribution']
        token_field = 'output_tokens'

    mu = dist_spec['params']['mu']
    sigma = dist_spec['params']['sigma']

    # Lognormal expected mean and std
    expected_mean = math.exp(mu + sigma**2 / 2)
    expected_std = math.sqrt((math.exp(sigma**2) - 1) * math.exp(2*mu + sigma**2))

    # Actual from trace
    tokens = [int(r[token_field]) for r in cohort_rows]
    actual_mean, actual_std, actual_min, actual_max = compute_stats(tokens)

    mean_diff = abs(actual_mean - expected_mean) / expected_mean * 100 if expected_mean > 0 else 0
    std_diff = abs(actual_std - expected_std) / expected_std * 100 if expected_std > 0 else 0

    verdict = 'PASS' if (mean_diff < 10 and std_diff < 20) else 'WARN' if (mean_diff < 15 and std_diff < 30) else 'FAIL'

    return {
        'expected_mean': expected_mean,
        'actual_mean': actual_mean,
        'mean_diff_pct': mean_diff,
        'expected_std': expected_std,
        'actual_std': actual_std,
        'std_diff_pct': std_diff,
        'actual_min': actual_min,
        'actual_max': actual_max,
        'verdict': verdict
    }

def validate_priority(cohort_spec, cohort_rows):
    """Validate SLO class and priority mapping."""
    slo_class = cohort_spec['slo_class']

    # Extract priorities from trace
    priorities = [int(r['vllm_priority']) if r['vllm_priority'] else None for r in cohort_rows]
    priorities = [p for p in priorities if p is not None]

    if not priorities:
        return {
            'slo_class': slo_class,
            'priorities': 'NONE',
            'verdict': 'FAIL'
        }

    # Check consistency
    unique_priorities = set(priorities)

    # Standard mapping (lower number = higher priority)
    expected_ranges = {
        'critical': (0, 2),
        'standard': (3, 4),
        'batch': (5, 6),
        'background': (7, 9),
        'sheddable': (7, 9)
    }

    expected_range = expected_ranges.get(slo_class, (0, 9))
    all_in_range = all(expected_range[0] <= p <= expected_range[1] for p in unique_priorities)

    verdict = 'PASS' if len(unique_priorities) == 1 and all_in_range else 'WARN' if all_in_range else 'FAIL'

    return {
        'slo_class': slo_class,
        'unique_priorities': sorted(unique_priorities),
        'expected_range': expected_range,
        'verdict': verdict
    }

def main():
    if len(sys.argv) < 3:
        print("Usage: validate_cohort_workload.py <workload.yaml> <data.csv>")
        sys.exit(1)

    workload_path = sys.argv[1]
    trace_path = sys.argv[2]

    print(f"Loading workload from: {workload_path}")
    print(f"Loading trace from: {trace_path}")
    print()

    workload = load_workload(workload_path)
    cohort_data = load_trace(trace_path)

    total_pass = 0
    total_warn = 0
    total_fail = 0

    for cohort_spec in workload['cohorts']:
        cohort_id = cohort_spec['id']
        cohort_rows = cohort_data.get(cohort_id, [])

        if not cohort_rows:
            print(f"❌ Cohort {cohort_id}: NO DATA FOUND")
            total_fail += 1
            continue

        print(f"{'='*80}")
        print(f"Cohort: {cohort_id}")
        print(f"{'='*80}")

        # 1. Arrival rate
        rate_result = validate_arrival_rate(cohort_spec, cohort_rows)
        print(f"\n1. Arrival Rate: {rate_result['verdict']}")
        print(f"   Expected: {rate_result['expected_rate']:.2f} req/s ({rate_result['expected_count']} requests)")
        print(f"   Actual:   {rate_result['actual_rate']:.2f} req/s ({rate_result['actual_count']} requests)")
        print(f"   Diff:     {rate_result['rate_diff_pct']:.1f}% (rate), {rate_result['count_diff_pct']:.1f}% (count)")

        if rate_result['verdict'] == 'PASS':
            total_pass += 1
        else:
            total_fail += 1

        # 2. Arrival process
        process_result = validate_arrival_process(cohort_spec, cohort_rows)
        if 'error' not in process_result:
            print(f"\n2. Arrival Process (Gamma): {process_result['verdict']}")
            print(f"   Mean IAT: expected={process_result['expected_mean_iat']:.6f}s, actual={process_result['actual_mean_iat']:.6f}s, diff={process_result['mean_iat_diff_pct']:.1f}%")
            print(f"   CV:       theoretical={process_result['theoretical_cv']:.4f}, actual={process_result['actual_cv']:.4f}, diff={process_result['cv_diff_pct']:.1f}%")
            print(f"   Skewness: theoretical={process_result['theoretical_skew']:.4f}, actual={process_result['actual_skew']:.4f}, diff={process_result['skew_diff_pct']:.1f}%")

            if process_result['verdict'] == 'PASS':
                total_pass += 1
            elif process_result['verdict'] == 'WARN':
                total_warn += 1
            else:
                total_fail += 1
        else:
            print(f"\n2. Arrival Process: SKIP ({process_result['error']})")

        # 3. Input tokens
        input_result = validate_token_distribution(cohort_spec, cohort_rows, 'input')
        print(f"\n3. Input Tokens (Lognormal): {input_result['verdict']}")
        print(f"   Mean: expected={input_result['expected_mean']:.1f}, actual={input_result['actual_mean']:.1f}, diff={input_result['mean_diff_pct']:.1f}%")
        print(f"   Std:  expected={input_result['expected_std']:.1f}, actual={input_result['actual_std']:.1f}, diff={input_result['std_diff_pct']:.1f}%")
        print(f"   Range: [{input_result['actual_min']}, {input_result['actual_max']}]")

        if input_result['verdict'] == 'PASS':
            total_pass += 1
        elif input_result['verdict'] == 'WARN':
            total_warn += 1
        else:
            total_fail += 1

        # 4. Output tokens
        output_result = validate_token_distribution(cohort_spec, cohort_rows, 'output')
        print(f"\n4. Output Tokens (Lognormal): {output_result['verdict']}")
        print(f"   Mean: expected={output_result['expected_mean']:.1f}, actual={output_result['actual_mean']:.1f}, diff={output_result['mean_diff_pct']:.1f}%")
        print(f"   Std:  expected={output_result['expected_std']:.1f}, actual={output_result['actual_std']:.1f}, diff={output_result['std_diff_pct']:.1f}%")
        print(f"   Range: [{output_result['actual_min']}, {output_result['actual_max']}]")

        if output_result['verdict'] == 'PASS':
            total_pass += 1
        elif output_result['verdict'] == 'WARN':
            total_warn += 1
        else:
            total_fail += 1

        # 5. Priority
        priority_result = validate_priority(cohort_spec, cohort_rows)
        print(f"\n5. Priority Mapping: {priority_result['verdict']}")
        print(f"   SLO Class: {priority_result['slo_class']}")
        print(f"   Priorities: {priority_result['unique_priorities']}")
        if 'expected_range' in priority_result:
            print(f"   Expected Range: {priority_result['expected_range']}")

        if priority_result['verdict'] == 'PASS':
            total_pass += 1
        elif priority_result['verdict'] == 'WARN':
            total_warn += 1
        else:
            total_fail += 1

        print()

    print(f"{'='*80}")
    print(f"SUMMARY: {total_pass} PASS, {total_warn} WARN, {total_fail} FAIL")
    print(f"{'='*80}")

if __name__ == '__main__':
    main()
