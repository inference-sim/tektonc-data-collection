#!/usr/bin/env python3
"""
Combine arrival patterns and workload distributions into BLIS workload files.

This script reads the separated arrival-and-workload-patterns.yaml file and
combines a specified workload with a specified arrival pattern to generate
a complete BLIS-consumable workload YAML file.
"""

import yaml
import argparse
import sys
from pathlib import Path


def combine_workload(patterns_file, workload_name, arrival_pattern, output_file=None, seed=42):
    """
    Combine arrival patterns and workload distributions into a BLIS workload.

    Args:
        patterns_file: Path to arrival-and-workload-patterns.yaml
        workload_name: Name of the workload (e.g., "m-mid")
        arrival_pattern: Name of the arrival pattern (e.g., "morning", "afternoon", "midnight")
        output_file: Path to output BLIS workload file (optional, skip file writing if None)
        seed: Random seed for the workload (default: 42)

    Returns:
        dict: BLIS workload structure with version, seed, cohorts, etc.
    """
    # Load the patterns file
    with open(patterns_file, 'r') as f:
        data = yaml.safe_load(f)

    arrival_patterns = data.get('arrival_patterns', {})
    workloads = data.get('workloads', {})

    # Validate inputs
    if arrival_pattern not in arrival_patterns:
        raise ValueError(
            f"Arrival pattern '{arrival_pattern}' not found. "
            f"Available: {', '.join(arrival_patterns.keys())}"
        )

    if workload_name not in workloads:
        raise ValueError(
            f"Workload '{workload_name}' not found. "
            f"Available: {', '.join(workloads.keys())}"
        )

    # Get the specific arrival pattern and workload
    arrival_data = arrival_patterns[arrival_pattern]
    workload_data = workloads[workload_name]

    # Check if the workload has data for this arrival pattern
    if arrival_pattern not in workload_data:
        raise ValueError(
            f"Workload '{workload_name}' does not have data for arrival pattern '{arrival_pattern}'. "
            f"Available patterns in this workload: {', '.join(workload_data.keys())}"
        )

    pattern_workload_data = workload_data[arrival_pattern]

    # Build the BLIS workload structure
    blis_workload = {
        'version': '2',
        'seed': seed,
        'category': '',
        'clients': [],
        'cohorts': [],
        'aggregate_rate': 0
    }

    # Get all SLO classes from the arrival pattern
    slo_classes = sorted(arrival_data.keys())

    for slo_class in slo_classes:
        # Check if this SLO class exists in both arrival and workload data
        if slo_class not in arrival_data:
            print(f"Warning: SLO class '{slo_class}' not found in arrival pattern, skipping",
                  file=sys.stderr)
            continue
        if slo_class not in pattern_workload_data:
            print(f"Warning: SLO class '{slo_class}' not found in workload data, skipping",
                  file=sys.stderr)
            continue

        arrival = arrival_data[slo_class]
        workload = pattern_workload_data[slo_class]

        # Combine into a cohort with all required fields
        cohort = {
            'id': f"{arrival_pattern}-{slo_class}",
            'population': arrival['population'],
            'slo_class': slo_class,
            'arrival': arrival['arrival'],
            'input_distribution': workload['input_distribution'],
            'output_distribution': workload['output_distribution'],
            'streaming': True,  # Default from original m-mid.yaml
            'rate_fraction': arrival['rate_fraction'],
            'spike': arrival['spike']
        }

        # Add reasoning-specific fields if present
        if 'reasoning' in workload:
            cohort['reasoning'] = workload['reasoning']

        # Add prefix_length if present (agentic workloads)
        if 'prefix_length' in workload:
            cohort['prefix_length'] = workload['prefix_length']

        # Add timeout if present (agentic workloads)
        if 'timeout' in workload:
            cohort['timeout'] = workload['timeout']

        # Add closed_loop field if present (default False for reasoning workloads, True for agentic)
        if 'closed_loop' in workload:
            cohort['closed_loop'] = workload['closed_loop']
        elif 'reasoning' in workload and 'multi_turn' in workload['reasoning']:
            # Agentic workloads with multi_turn are closed_loop by default
            cohort['closed_loop'] = True

        blis_workload['cohorts'].append(cohort)

    # Write the output file if path provided
    if output_file is not None:
        with open(output_file, 'w') as f:
            yaml.dump(blis_workload, f, default_flow_style=False, sort_keys=False)

        print(f"✓ Successfully created BLIS workload file: {output_file}")
        print(f"  Workload: {workload_name}")
        print(f"  Arrival pattern: {arrival_pattern}")
        print(f"  Cohorts: {len(blis_workload['cohorts'])} ({', '.join([c['id'] for c in blis_workload['cohorts']])})")

    return blis_workload


def list_available(patterns_file):
    """List all available workloads and arrival patterns."""
    with open(patterns_file, 'r') as f:
        data = yaml.safe_load(f)

    arrival_patterns = data.get('arrival_patterns', {})
    workloads = data.get('workloads', {})

    print("Available arrival patterns:")
    for pattern in arrival_patterns.keys():
        slo_classes = list(arrival_patterns[pattern].keys())
        print(f"  - {pattern} (SLO classes: {', '.join(slo_classes)})")

    print("\nAvailable workloads:")
    for workload_name, workload_data in workloads.items():
        periods = list(workload_data.keys())
        print(f"  - {workload_name} (periods: {', '.join(periods)})")


def main():
    parser = argparse.ArgumentParser(
        description='Combine arrival patterns and workload distributions into a BLIS workload file',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create morning workload with m-mid distributions
  %(prog)s --workload m-mid --arrival-pattern morning -o morning-workload.yaml

  # Create afternoon workload
  %(prog)s --workload m-mid --arrival-pattern afternoon -o afternoon-workload.yaml

  # Create midnight workload with custom seed
  %(prog)s --workload m-mid --arrival-pattern midnight --seed 123 -o midnight-workload.yaml

  # List available workloads and arrival patterns
  %(prog)s --list
        """
    )

    parser.add_argument(
        '--patterns-file',
        type=str,
        default='arrival-and-workload-patterns.yaml',
        help='Path to arrival-and-workload-patterns.yaml file (default: %(default)s)'
    )

    parser.add_argument(
        '--workload',
        type=str,
        help='Workload name (e.g., m-mid)'
    )

    parser.add_argument(
        '--arrival-pattern',
        type=str,
        help='Arrival pattern name (e.g., morning, afternoon, midnight)'
    )

    parser.add_argument(
        '-o', '--output',
        type=str,
        help='Output BLIS workload file path'
    )

    parser.add_argument(
        '--seed',
        type=int,
        default=42,
        help='Random seed for the workload (default: %(default)s)'
    )

    parser.add_argument(
        '--list',
        action='store_true',
        help='List available workloads and arrival patterns'
    )

    args = parser.parse_args()

    # Check if patterns file exists
    if not Path(args.patterns_file).exists():
        print(f"Error: Patterns file not found: {args.patterns_file}", file=sys.stderr)
        return 1

    # List mode
    if args.list:
        try:
            list_available(args.patterns_file)
            return 0
        except Exception as e:
            print(f"Error listing patterns: {e}", file=sys.stderr)
            return 1

    # Combine mode - require all arguments
    if not args.workload or not args.arrival_pattern or not args.output:
        parser.error("--workload, --arrival-pattern, and --output are required (unless using --list)")

    try:
        combine_workload(
            patterns_file=args.patterns_file,
            workload_name=args.workload,
            arrival_pattern=args.arrival_pattern,
            output_file=args.output,
            seed=args.seed
        )
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
