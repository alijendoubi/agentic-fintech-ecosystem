# Seed scenarios for the Red debater (ALI-157)

`historical_scenarios.json` holds curated, public market events that the Red debater can recall as
adversarial precedent. They are **reference data, not results**: nothing here was produced by this
system, and no entry claims a calibrated or backtested effect.

**Status: DRAFT.** TODO(owner): review every entry (facts, dates, regime labels, lessons) and open
every source link, then set `"status": "APPROVED"`. The links were written from general knowledge
and could not be opened from the build sandbox (egress blocked), so they are unverified. The loader
refuses a DRAFT file unless `--allow-draft` is passed, which is for dev/test stores only.

The original ALI-39 spec asked for 500 scenarios; this is a first set of 16 (31 records). Add
entries in the same format; the validator rejects the whole file on any bad entry.

## Format (`schema_version` 1)

```json
{"schema_version": 1, "status": "DRAFT | APPROVED", "entries": [{
  "id": "kebab-case-id", "title": "...", "date": "YYYY-MM-DD",
  "regimes": ["TRENDING_BULL | TRENDING_BEAR | HIGH_VOL_CHOP | LOW_VOL_CHOP | CRISIS", "..."],
  "what_happened": "...", "failure_pattern": "...", "lesson_for_red": "...",
  "sources": ["https://..."]}]}
```

Each entry becomes one memory record per listed regime (recall matches on exact regime):
`record_id = seed:<id>:<regime>`, `kind = seed_scenario`, `symbol = MARKET`, `outcome = unknown`.
Seed records never expire (age-based retention would drop historical dates on load); reloading
the file upserts the same ids.

## Commands (from `agent-core/zone-a/vector-db`)

```bash
python -m afe_vector_memory.seed validate seeds/historical_scenarios.json
python -m afe_vector_memory.seed load seeds/historical_scenarios.json            # APPROVED only
python -m afe_vector_memory.seed load seeds/historical_scenarios.json --allow-draft  # dev store
```

`load` uses the same `VectorMemorySettings` environment as the runtime store.
