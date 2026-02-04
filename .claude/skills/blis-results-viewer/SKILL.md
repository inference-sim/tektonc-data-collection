---
name: blis-results-viewer
description: Use when asked to view, analyze, or display results from a BLIS data collection experiment. Triggers include "show results", "download data", "view benchmark", or referencing an experiment ID.
---

# BLIS Results Viewer

Displays benchmark results from BLIS LLM data collection experiments.

## Data Location

Results are in `results/<experiment-id>/data/guidellm-results.json`

## File Cleanup

Downloaded files may have kubectl artifacts appended. Clean before parsing:

```bash
# Remove pod deletion message if present
sed -i '' 's/pod ".*" deleted from .* namespace$//' guidellm-results.json
```

## GuideLLM JSON Structure

**Critical:** The structure differs from what you might expect:

```
{
  "args": { "target": "...", "backend": "openai_http", "profile": "sweep" },
  "benchmarks": [
    {
      "config": {
        "strategy": {
          "type_": "synchronous|throughput|constant",
          "rate": <float>  # only for constant type
        }
      },
      "requests": {
        "successful": [...],  # NOT "completed"
        "errored": [...]
      },
      "duration": <float>
    }
  ]
}
```

**Key gotchas:**
- Requests are in `requests.successful`, NOT `requests.completed`
- Metrics are per-request, NOT aggregated at benchmark level
- Time fields use `_ms` suffix: `time_to_first_token_ms`, `inter_token_latency_ms`
- Exception: `request_latency` is in **seconds** (multiply by 1000)

## Request Object Fields

| Field | Unit | Description |
|-------|------|-------------|
| `request_latency` | **seconds** | Total request time |
| `time_to_first_token_ms` | ms | Time to first token |
| `inter_token_latency_ms` | ms | Average inter-token latency |
| `output_tokens_per_second` | tok/s | Per-request throughput |
| `prompt_tokens` | count | Input tokens |
| `output_tokens` | count | Generated tokens |

## Parsing Code

```python
import json

with open('results/<experiment>/data/guidellm-results.json') as f:
    data = json.load(f)

for bench in data.get('benchmarks', []):
    strategy = bench.get('config', {}).get('strategy', {})
    strategy_type = strategy.get('type_', '')
    rate = strategy.get('rate', 0)

    # Rate label
    rate_str = 'sync' if strategy_type == 'synchronous' else \
               'max' if strategy_type == 'throughput' else f'{rate:.1f}'

    # Get successful requests (NOT 'completed')
    successful = bench.get('requests', {}).get('successful', [])

    # Aggregate metrics
    latencies = [r['request_latency'] * 1000 for r in successful if r.get('request_latency')]
    ttfts = [r['time_to_first_token_ms'] for r in successful if r.get('time_to_first_token_ms')]
    itls = [r['inter_token_latency_ms'] for r in successful if r.get('inter_token_latency_ms')]
    throughputs = [r['output_tokens_per_second'] for r in successful if r.get('output_tokens_per_second')]

    # Means
    avg_lat = sum(latencies) / len(latencies) if latencies else 0
    avg_ttft = sum(ttfts) / len(ttfts) if ttfts else 0
    avg_itl = sum(itls) / len(itls) if itls else 0
    avg_tp = sum(throughputs) / len(throughputs) if throughputs else 0

    print(f"{rate_str:>8} {len(successful):>5} {avg_lat:>8.0f}ms {avg_ttft:>6.0f}ms {avg_itl:>5.1f}ms {avg_tp:>6.0f}")
```

## Percentiles

```python
latencies.sort()
p50 = latencies[len(latencies) // 2]
p99 = latencies[int(len(latencies) * 0.99)]
```

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `JSONDecodeError: Extra data` | Pod message appended | Run sed cleanup |
| All metrics show 0 | Wrong field names | Use `requests.successful` |
| Latency too small | Forgot *1000 | `request_latency` is seconds |
