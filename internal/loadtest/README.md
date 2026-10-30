# loadtest (repo-internal)

Comparison driver for flotilla bring-up/performance sweeps against alternative arms
(DinD wrapper, bare platform API). Lives outside the published package; nothing here is
shipped in the wheel.

## Environment

All deployment-specific values come from args or environment — none are committed:

| variable | used by | meaning |
|---|---|---|
| `FLOTILLA_DEPLOYMENT` | driver, arms, probes | path to `deployment.toml` |
| `OPENSANDBOX_SECRET_FILE` | driver, arms, probes | private env file with `OPEN_SANDBOX_ACCESS_KEY`/`OPEN_SANDBOX_SECRET_KEY` (0600) |
| `OPENSANDBOX_ENDPOINT` | observer, mirror | platform API base URL |
| `OPENSANDBOX_PROJECT` | observer, mirror | workspace name |

## Modules

| module | role |
|---|---|
| `events.py` | append-only `loadtest-event-v1` JSONL; the single data source for all arms |
| `observer.py` | independent process: quotas + active inventory + terminated history each tick |
| `flotilla_arm.py` | arm A: one `Flotilla` client, one environment per episode, timings recorded |
| `episode.py` | shared episode loop; `compat` (bring-up) and `perf` (+100 KiB upload, 20-exec mix) |
| `run_compat.py` | checkpointed sweep driver (event log is the checkpoint; STOP-file aware) |
| `report.py` | compatibility matrix + stage percentile summary from an event log |
| `kata_gate.py` | execd /ping readiness shim for kata instances (see `Execd.ping` fix PR) |
| `creds.py` | credential binding ported from the training-side provider |
| `p0_privileged.py` | P0: privileged kata DinD bring-up validation; flips `privileged_runtime` |
| `p0_udp.py` | P0: UDP datagram round-trip; flips `link_udp` |
| `mirror_dind_image.py` | copy a platform-only image into a dev-reachable registry via kata transfer sandbox |
| `tasks/` | probe task templates (`p0-udp`; privileged dind task is created per deployment) |

## Typical run

```sh
export FLOTILLA_DEPLOYMENT=.../deployments/<name>/deployment.toml
export OPENSANDBOX_SECRET_FILE=.../.env
export OPENSANDBOX_ENDPOINT=https://... OPENSANDBOX_PROJECT=...

python -I internal/loadtest/observer.py --out obs.jsonl --interval 30 &
python -I internal/loadtest/run_compat.py \
    --registry .../registry.json --events events.jsonl \
    --limit 20 --shuffle 7 --concurrency 4 --mode perf
python -I internal/loadtest/report.py --events events.jsonl --out report.md
```
