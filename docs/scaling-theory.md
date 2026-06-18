# Scaling Theory for LLM Serving on Constrained Hardware

## The core intuition

Scaling pods on a fixed machine is like adding more cooks to a kitchen that
has a fixed number of stoves. Up to a point, more cooks means more meals per
hour. Past that point, cooks start waiting for stoves, bumping into each other,
and total output actually drops. There is a sweet spot — the maximum number of
cooks the kitchen can support before congestion costs more than the extra hands
gain.

For LLM serving the "stoves" are CPU cores and RAM. The "meals" are inference
requests. The question this document answers is: **given a machine's specs, how
many replicas can you run before performance degrades?**

---

## The three resource ceilings

Every machine has three independent ceilings. Scaling hits whichever one it
reaches first.

### Ceiling 1 — RAM

The hardest ceiling. Unlike CPU, RAM cannot be time-shared. If a model does not
fit in RAM it cannot run at all. There is no graceful degradation — the OS
starts swapping to disk and inference speed drops by 100x or more.

```
max_replicas_ram = floor(available_ram / ram_per_replica)
```

**Example — 128GB server, Qwen3-32B at Q5_K_M (~22GB):**
```
max_replicas_ram = floor((128 - 8) / 22) = floor(120 / 22) = 5 replicas
```
Reserve ~8GB for OS, Redis, workers, and KV cache headroom.

**Example — 16GB MacBook, SmolLM2-135M (~150MB):**
```
max_replicas_ram = floor((16 - 4) / 0.15) = floor(12 / 0.15) = 80 replicas
```
RAM is not the constraint here. CPU is.

### Ceiling 2 — CPU cores / threads

CPU inference is purely compute-bound. llama.cpp uses `--threads N` to control
how many physical cores it uses per model instance. If you run more replicas
than you have cores to feed them, threads compete and each gets less CPU time.

```
max_replicas_cpu = floor(physical_cores / threads_per_replica)
```

**Example — i7-9750H (6 physical cores), --threads 4:**
```
max_replicas_cpu = floor(6 / 4) = 1 replica
```
One replica uses 4 cores cleanly. Two replicas would each get 3 cores (if the
OS splits evenly) but with contention overhead — effectively slower than one.

**Example — 128GB server with 32 physical cores, --threads 8:**
```
max_replicas_cpu = floor(32 / 8) = 4 replicas
```

A note on hyperthreading: physical cores matter, not logical threads. A 6-core
i7 with hyperthreading reports 12 logical CPUs but only has 6 physical cores.
For compute-intensive workloads like matrix multiplication (which is all LLM
inference is), hyperthreads share the same execution units. Setting
`--threads 12` on a 6-core machine does not help and can hurt.

### Ceiling 3 — Memory bandwidth

This is the least obvious ceiling and the one most people miss.

LLM inference is memory-bandwidth-bound, not compute-bound. The bottleneck is
not multiplying numbers — it is moving the model weights from RAM to the CPU
cache on every token. A larger model moves more data per token. More replicas
means more concurrent weight reads. At some point, all replicas are waiting for
data from RAM and adding more replicas adds zero throughput.

```
tokens_per_second ≈ memory_bandwidth / model_size_bytes
```

**Example — DDR4-3200 (51 GB/s bandwidth), Qwen3-32B (22GB):**
```
theoretical max ≈ 51 / 22 ≈ 2.3 tokens/sec per replica at full bandwidth
```

Two replicas would each get ~1.15 tokens/sec if they saturate bandwidth
together — same total throughput as one replica, but each request takes twice
as long. This is where scaling stops helping.

Memory bandwidth is why Apple Silicon (M-series) is unusually good at LLM
inference: unified memory architecture gives 200-800 GB/s bandwidth vs 50 GB/s
for typical DDR4. A MacBook M4 Max can run models faster than a server with
10x the RAM.

---

## Amdahl's Law — why you can't scale forever

Amdahl's Law says: if a fraction `p` of your work can be parallelised, the
maximum speedup from N parallel units is:

```
speedup = 1 / ((1 - p) + p/N)
```

For LLM inference, the serial fraction includes:
- Token sampling (must happen sequentially)
- KV cache reads for prior context (sequential per request)
- Redis round-trips (serial per task)

Even if inference itself parallelised perfectly (it doesn't), these serial
portions cap your speedup. If 20% of work is serial:

```
N=1:   speedup = 1.0×
N=2:   speedup = 1.67×
N=4:   speedup = 2.5×
N=8:   speedup = 3.3×
N=16:  speedup = 4.0×
N=∞:   speedup = 5.0×  ← hard ceiling, never exceeded
```

Doubling replicas gives diminishing returns. The first replica doubles
throughput. The eighth adds 3% over the seventh.

---

## Little's Law — sizing the queue and replicas together

Little's Law is the most practical tool for capacity planning:

```
L = λ × W
```

Where:
- `L` = average number of requests in the system (queue + being processed)
- `λ` = arrival rate (requests per second)
- `W` = average time to complete one request (seconds)

**Rearranged for replica count:**

If you know your arrival rate and your per-replica throughput:

```
replicas_needed = ceil(λ / throughput_per_replica)
```

**Example:**

SmolLM2-360M generates ~68 tokens/sec on one replica.
A typical response is ~50 tokens, so one request takes ~0.74 seconds.
You expect 10 requests/second peak load.

```
replicas_needed = ceil(10 / (1/0.74)) = ceil(10 × 0.74) = ceil(7.4) = 8
```

But wait — 8 llm-360m replicas × ~338MB = ~2.7GB RAM. Fine on 128GB.
Check CPU: 8 replicas × 4 threads = 32 threads needed.
On a 32-core server: exactly at the CPU ceiling. One more replica and you start
thrashing.

This is the sweet spot: 8 replicas hits the CPU ceiling without exceeding it.

---

## The USL — Universal Scalability Law

Amdahl's Law misses one thing: **contention overhead**. When multiple replicas
share resources (CPU cache, memory bus, OS scheduler), they don't just fail to
help each other — they actively slow each other down.

Neil Gunther's Universal Scalability Law adds a contention penalty:

```
throughput(N) = N / (1 + α(N-1) + βN(N-1))
```

Where:
- `α` = contention coefficient (resources fighting each other)
- `β` = coherency coefficient (coordination overhead)

For LLM pods on a single machine, `α` is high (memory bandwidth contention)
and `β` is low (no inter-pod coordination needed). The result looks like this:

```
Throughput
    ▲
    │        ● peak
    │      ●   ●
    │    ●       ●
    │  ●           ●  ← contention degrades total throughput
    │●               ●●●
    └─────────────────────► N replicas
         sweet spot ↑
```

Total throughput rises, peaks at the sweet spot, then falls as contention
costs exceed the parallelism gains. This is exactly the "too many apps on a
laptop" phenomenon you described.

The peak occurs at:

```
N_optimal = sqrt((1 - α) / β)
```

In practice you find this empirically (see benchmarking section below) because
`α` and `β` are hard to measure analytically for a given machine.

---

## The resource allocation formula

Combining all three ceilings:

```
sweet_spot = min(
    floor(available_ram / ram_per_replica),          # RAM ceiling
    floor(physical_cores / threads_per_replica),     # CPU ceiling
    floor(memory_bandwidth / bandwidth_per_replica)  # bandwidth ceiling
)
```

Then apply a safety margin of 0.7-0.8 to avoid operating at the exact ceiling
(OS and other processes need headroom):

```
safe_replicas = floor(sweet_spot × 0.75)
```

### Worked example — 128GB server, Qwen3-32B

```
Machine specs:
  RAM:               128 GB
  Physical cores:    32
  Memory bandwidth:  ~100 GB/s (DDR5-4800, 2 channels)

Model specs:
  RAM per replica:   22 GB (Q5_K_M)
  Threads:           8
  Bandwidth usage:   ~22 GB/s per replica at peak

Ceilings:
  RAM:       floor((128 - 12) / 22) = floor(116 / 22) = 5
  CPU:       floor(32 / 8)          = 4
  Bandwidth: floor(100 / 22)        = 4

sweet_spot = min(5, 4, 4) = 4

safe_replicas = floor(4 × 0.75) = 3
```

Run 3 replicas of Qwen3-32B. The 4th would push you to the CPU and bandwidth
ceilings simultaneously, causing contention.

### Worked example — 16GB MacBook, SmolLM2-135M

```
Machine specs:
  RAM:               16 GB
  Physical cores:    6
  Memory bandwidth:  ~51 GB/s (DDR4-3200)

Model specs:
  RAM per replica:   0.15 GB
  Threads:           4
  Bandwidth usage:   ~0.15 GB/s per replica

Ceilings:
  RAM:       floor((16 - 4) / 0.15) = 80
  CPU:       floor(6 / 4)            = 1
  Bandwidth: floor(51 / 0.15)        = 340

sweet_spot = min(80, 1, 340) = 1
safe_replicas = 1
```

CPU is the binding constraint. One llm-135m replica with `--threads 4` is
optimal. A second replica would each get 3 cores and be slower than one with 4.

The practical answer: increase `--parallel` slots on the single replica
(vertical scaling) rather than adding replicas (horizontal scaling).

---

## The --parallel sweet spot inside one replica

Each parallel slot in llama.cpp needs its own KV cache allocation:

```
kv_cache_per_slot = ctx_size × num_layers × head_dim × 2 × dtype_bytes
```

Simplified approximation:

```
kv_cache_total ≈ ctx_size × parallel_slots × model_size × 0.02
```

For SmolLM2-360M, ctx=2048, 4 slots:
```
kv_cache ≈ 2048 × 4 × 338MB × 0.02 ≈ 55MB
```

Negligible. You can run `--parallel 8` on this model without RAM issues.

For Qwen3-32B, ctx=8192, 4 slots:
```
kv_cache ≈ 8192 × 4 × 22000MB × 0.02 ≈ 14GB
```

Significant. With 3 replicas × 14GB = 42GB KV cache on top of 66GB weights =
108GB total. Fits in 128GB with 20GB headroom. Tight but viable.

---

## Benchmarking to find your empirical sweet spot

Theory gives you a starting point. Measurement gives you the actual number.

### Step 1 — baseline single replica

```bash
# Send 10 sequential requests, record tokens/sec from llama.cpp timings
for i in {1..10}; do
  curl -s -X POST http://localhost:8001/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"local","messages":[{"role":"user","content":"Count to 20"}],"max_tokens":50}' \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['timings']['predicted_per_second'])"
done | awk '{sum+=$1; n++} END {print "avg tokens/sec:", sum/n}'
```

### Step 2 — load test with N=1 replica

Use the dashboard Load Tester with 5 concurrent requests. Record:
- Average elapsed time
- Tokens/sec per task
- Total throughput (sum of all tokens / total elapsed)

### Step 3 — scale to N=2, repeat

```bash
kubectl scale deployment/llm-135m --replicas=2
# wait for ready
kubectl rollout status deployment/llm-135m
# repeat load test
```

### Step 4 — plot throughput vs replicas

```
N=1: total_throughput = X tok/s
N=2: total_throughput = Y tok/s
N=3: total_throughput = Z tok/s
...
```

Stop when adding a replica increases total throughput by less than 10% or
decreases it. That is your empirical sweet spot.

### What to watch during the test

```bash
# CPU usage per core (watch for saturation)
top -d 1

# Memory usage
free -h

# llama.cpp slot utilisation (are all parallel slots busy?)
curl http://localhost:8001/slots | python3 -m json.tool
```

---

## Rules of thumb for CPU-only inference

These hold across most LLM models on commodity server hardware:

| Rule | Reason |
|---|---|
| `--threads` = physical cores ÷ replicas | Never share cores between replicas |
| `--threads` ≤ physical cores ÷ 2 | Leave headroom for OS and other services |
| `--parallel` = 4 is a good default | Balances KV cache RAM vs concurrency |
| Replicas × RAM < 75% of total RAM | Leave headroom for KV cache and OS |
| Stop scaling when tok/s gain < 10% per replica | You've hit the USL peak |
| On < 8 cores: prefer 1 replica, more `--parallel` | CPU ceiling too low for multiple replicas |
| On ≥ 32 cores: 2-4 replicas is typically optimal | Beyond 4, bandwidth contention dominates |

---

## Applied to your two machines

### 16GB MacBook (i7-9750H, 6 cores)

```
SmolLM2-135M:  1 replica, --threads 4, --parallel 4
SmolLM2-360M:  1 replica, --threads 4, --parallel 4
               (running both simultaneously: use --threads 3 each)

Reason: CPU ceiling is 1 replica per 4-thread model.
        Vertical scaling (--parallel) is the only lever here.
        The dashboard load tester is for validating architecture,
        not for finding a scaling sweet spot — there is none at 6 cores.
```

### 128GB server (32 cores, CPU-only)

```
Qwen3-32B (orchestrator/analysis):
  3 replicas, --threads 8, --parallel 4
  RAM: 3 × 22GB = 66GB weights + ~42GB KV cache = 108GB ✓
  CPU: 3 × 8 = 24 threads (leaves 8 for OS + workers + Redis) ✓

Qwen3-8B (ingestion/summary):
  4 replicas, --threads 4, --parallel 4
  RAM: 4 × ~5GB = 20GB ✓
  CPU: 4 × 4 = 16 threads

Total CPU: 24 + 16 = 40 threads on 32 physical cores → over budget
Adjust: Qwen3-32B to 2 replicas (16 threads) + Qwen3-8B to 4 replicas (16 threads) = 32 total ✓

Final allocation:
  Qwen3-32B:  2 replicas, --threads 8,  --parallel 4  → 44GB weights
  Qwen3-8B:   4 replicas, --threads 4,  --parallel 4  → 20GB weights
  Redis:      1GB
  Workers:    8 × 50MB = 400MB
  OS + misc:  8GB
  KV cache:   ~30GB (estimated)
  Total:      ~104GB of 128GB ✓
```

---

## Summary

The sweet spot is the minimum of three ceilings — RAM, CPU cores, and memory
bandwidth — with a 25% safety margin applied. Theory (Amdahl, USL) tells you
the shape of the curve: rising throughput that peaks then falls as contention
overwhelms parallelism. Measurement (load tester + slot utilisation) tells you
where your specific machine's peak actually sits.

For CPU-only inference, memory bandwidth is the binding constraint at scale.
For small models on few cores, CPU thread count is the binding constraint.
RAM is rarely the binding constraint unless the model is large relative to
available memory.

When in doubt: start with 1 replica, measure, add one, measure again. Stop when
the gain drops below 10%. That is your sweet spot.