# ADR: EJ transport — keep the agent, keep EJ out of Zabbix

> **Status:** Accepted, 2026-10-16. Recorded after a review question asked whether
> the server should stop pulling EJ journals from the agents and the agents should
> push instead. **Decision: do not build it.** Revisit only on the trigger below.

## The question

> *"Instead of the server pulling, why don't you make the agent push? The pull is
> a passive agent check, which is the wrong mode for a large, firewalled ATM fleet.
> Keep the agent; change the mode — active checks / trapper items."*

The instinct is sound. The mechanism diagnosis, and applying it to EJ, are not.

## What the pull actually is

`scripts/pull_ej_logs.sh` calls:

```bash
zabbix_get -s "$ip" -k "vfs.file.contents[E:\\EJLogs\\$f]"
```

`zabbix_get -s <ip>` opens a TCP connection **directly to that agent on :10050**.
It is not a Zabbix passive check — it bypasses the Zabbix server, its queue, its
cache and item state entirely. There is no passive check here to convert to an
active one.

This matters because "change the mode, keep the agent" is the right instinct
applied to the wrong starting point.

## EJ is not the network problem

The real server→ATM load is the WMI template polling **~90 items per NCR host
every 10–30s** — about 4,000–12,000 collections/sec at 1,300 ATMs. EJ adds **one
file read per ATM per minute** (~22/sec), under 1% of the polling budget.

If firewall topology is the concern, EJ pull is not the cause.

## Two mechanisms were conflated

| | Direction | Purpose |
|---|---|---|
| Active agent checks | agent → server (buffers locally, pushes on schedule) | Firewalled sites the server cannot reach |
| Trapper items | script → server on demand (`zabbix_sender`) | Pushing *events*, not polling metrics |

Not interchangeable. The proposal treats them as one thing.

## Why EJ in Zabbix would be a regression

1. **PCI.** EJ lines carry card data. The parser masks PANs before anything is
   stored. Push EJ in as item values and **raw lines sit in the value table** —
   history, trends, trigger macros — for the retention period. That is a
   compliance regression on the one data class that must never be persisted
   unmasked.
2. **No search.** Zabbix has no full-text search over item values. EJ Search runs
   on OpenSearch; the feature would be lost.
3. **Item explosion.** One item per EJ record = thousands of items per ATM.
   Zabbix's item count is a bounded resource.
4. **Retention mismatch.** Zabbix keeps days-to-months. Dispute evidence needs
   years.

EJ belongs in `filebeat → OpenSearch`, which is what already runs.

## What was agreed instead

| Concern | Decision |
|---|---|
| Keep the agent | **Yes** — unchanged |
| EJ transport | Keep the pull (measured 0.138 s/fetch, ~22 s for 1,300 ATMs) |
| Metrics topology | Proxies or active agents — already planned in `docs/proxy-topology.md` |
| `zabbix_get` lacks backoff | The cron owns the lock and concurrency; that stays our job, not 1,300 Windows boxes' |

## The one trigger that changes this

**If the monitoring host cannot reach a branch on TCP/10050, EJ pull dies there
too** and an agent→server channel is required. The minimum viable version:

> The agent POSTs the journal file to a small receiver on the monitoring host,
> which writes it into the **same directory `filebeat` already watches**.

That removes the cron's dependence on `zabbix_get`, works behind firewalls, and
touches neither Zabbix nor the OpenSearch pipeline.

## Before rollout — nobody has asked this

**Confirm with Network whether the monitoring host can reach every branch on
TCP/10050.** That single answer decides whether any of the above matters. If
some branches cannot, the split is:

- metrics → Zabbix proxy or active agent for those branches
- EJ → the file-POST receiver

## Operational note

`zabbix_get` has no retry, backoff or rate limiting of its own. The cron supplies
the lock, the concurrency cap and the bounded parallelism. Pushing moves that
responsibility onto 1,300 machines where it cannot be observed or fixed
centrally — an argument against push on operational grounds, independent of
everything above.