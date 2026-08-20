#!/usr/bin/env python3
"""Analyze runtime health and latency metrics from ORC trace."""

import csv
import sys
import statistics

def analyze_runtime(trace_path):
    """Analyze request statuses and latency."""
    statuses = {}
    latencies = []

    with open(trace_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            status = row['status']
            statuses[status] = statuses.get(status, 0) + 1

            if status == 'ok':
                lat_ms = (int(row['last_chunk_time_us']) - int(row['send_time_us'])) / 1000
                latencies.append(lat_ms)

    # Status breakdown
    total = sum(statuses.values())
    print("Request Status:")
    for status, count in sorted(statuses.items()):
        pct = count / total * 100
        print(f"  {status}: {count} ({pct:.1f}%)")

    print()

    # Latency percentiles
    if latencies:
        latencies.sort()
        n = len(latencies)
        p50 = latencies[int(n * 0.50)]
        p90 = latencies[int(n * 0.90)]
        p99 = latencies[int(n * 0.99)]

        print("End-to-End Latency (ms):")
        print(f"  p50: {p50:.1f}")
        print(f"  p90: {p90:.1f}")
        print(f"  p99: {p99:.1f}")
        print(f"  min: {min(latencies):.1f}")
        print(f"  max: {max(latencies):.1f}")

        # Flags
        if p99 > 60000:
            print(f"  ⚠️ p99 exceeds 60s (likely timeout)")
        if p50 > 10000:
            print(f"  ⚠️ p50 exceeds 10s (suspiciously slow)")

    # Error rate verdict
    error_count = sum(c for s, c in statuses.items() if s != 'ok')
    error_rate = error_count / total * 100

    print()
    print(f"Error Rate: {error_rate:.2f}%")
    if error_rate > 1.0:
        print("  ⚠️ Error rate > 1% (high for calibration workload)")
    else:
        print("  ✓ Error rate acceptable")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: analyze_runtime.py <data.csv>")
        sys.exit(1)

    analyze_runtime(sys.argv[1])
