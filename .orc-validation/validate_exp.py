import csv, yaml, math, statistics, sys

RUN = "campaign/1-qwen-qwen3-14b-h100-general/data/1-qwen-qwen3-14b-tp1-general-1-1"
wl = yaml.safe_load(open(f"{RUN}/observe/workload.yaml"))
ip = wl["inference_perf"]; sp = ip["shared_prefix"]; stages = ip["stages"]
mt = sp.get("enable_multi_turn_chat", False)

rows = list(csv.DictReader(open(f"{RUN}/observe/data.csv")))
def col(n): return [r[n] for r in rows]
def inti(n): return [int(r[n]) for r in rows if r[n] not in ("", None)]

print(f"MULTI_TURN={mt}")
print(f"stages={stages}")
exp_total = sum(int(s['rate']*s['duration']) for s in stages)
NumSessions = sp['num_unique_system_prompts']*sp['num_users_per_system_prompt']
print(f"\n== 1d/1a Request count ==")
print(f"expected(rate*dur)={exp_total}  actual={len(rows)}  NumSessions={NumSessions}")

print(f"\n== 1b Token distributions ==")
it = inti('input_tokens'); ot = inti('output_tokens')
print(f"input_tokens: mean={statistics.mean(it):.1f} std={statistics.pstdev(it):.1f} min={min(it)} max={max(it)}  (spec question_len={sp['question_len']}, +prefix {sp['system_prompt_len']})")
print(f"output_tokens: mean={statistics.mean(ot):.1f} std={statistics.pstdev(ot):.1f} min={min(ot)} max={max(ot)}  (spec output_len={sp['output_len']})")

print(f"\n== 1c Prefix tokens ==")
pl = inti('prefix_length')
nz = sum(1 for x in pl if x>0)
print(f"prefix_length>0: {nz}/{len(pl)} = {100*nz/len(pl):.1f}%  (multi-turn: expect ~NumSessions*stages/total)")
from collections import Counter
print("prefix_length distribution:", dict(Counter(pl).most_common(5)))

print(f"\n== 3c Request status ==")
st = Counter(col('status'))
print("status:", dict(st))
fr = Counter(col('finish_reason'))
print("finish_reason:", dict(fr))

print(f"\n== 3f Latency (e2e = last_chunk - send, ms) ==")
lat = sorted((int(r['last_chunk_time_us'])-int(r['send_time_us']))/1000 for r in rows if r['last_chunk_time_us'] and r['send_time_us'] and int(r['last_chunk_time_us'])>0)
def pct(p): 
    i=int(len(lat)*p/100); return lat[min(i,len(lat)-1)]
print(f"n={len(lat)} p50={pct(50):.0f}ms p90={pct(90):.0f}ms p99={pct(99):.0f}ms max={lat[-1]:.0f}ms")

print(f"\n== 3b Instance / slo / priority columns ==")
print("slo_class values:", dict(Counter(col('slo_class'))))
print("vllm_priority values:", dict(Counter(col('vllm_priority'))))
