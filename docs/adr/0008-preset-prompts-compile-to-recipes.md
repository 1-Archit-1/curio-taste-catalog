# 8. Preset prompts compile into stored recipes

Status: Proposed. Narrows the pause scope in [ADR 0002](0002-per-user-api-keys.md) §5.

## Context

A preset has a free-text prompt, for example "melancholic road thrillers", "movies like Bodies Bodies Bodies", or "shows which are christmas themed". Different prompts need different retrieval:

- A seed title ("like X") must be resolved to a TMDB ID, then its recommendations and similar titles fetched.
- A topic ("christmas themed") maps to TMDB keyword IDs and a discover query.
- A mood ("melancholic") has no good TMDB filter and needs embedding similarity.
- A vague prompt ("something I'd love", "hidden gems") gives discover almost nothing to work with, so the user's taste has to lead retrieval.

Two pipelines generate rows. The agent (#8) calls an LLM. The baseline (#7) calls no chat LLM, runs the automatic low-pool refills, and is the comparison point for the evals (#11). Until now only the agent read the prompt, so baseline rows looked the same whatever the prompt said.

Only workers can decrypt API keys (ADR 0002), so any LLM call that interprets a prompt must run as a worker job, not in the API request that saves the preset.

## Decision

### 1. Recipes

When a prompt is saved or edited, a worker job asks the LLM, once and with structured output, to turn it into a **recipe**, then resolves names to TMDB IDs through TMDB search. The recipe is stored on the preset (JSON with a schema version), shown in the Catalogs tab, and editable. The column `presets.criteria` is renamed `prompt`.

A recipe holds:

- **Prompt type**: one of `like_title`, `topic`, `mood`, `taste_led`, `mixed`.
- **Seed titles** as TMDB IDs.
- **Hard constraints**, which every candidate must pass: required and excluded keyword and genre IDs, year range, original language, runtime range, minimum vote count, popularity ceiling.
- **Soft preferences**, which only affect scoring: preferred keywords, genres and people, and a short mood text.
- **Overrides** of source quotas and scoring weights. Only a user edit sets these; the compile job never does.

### 2. Compile rules

- **Resolution failures.** A keyword or person that can't be resolved is dropped and listed as a warning on the recipe. An ambiguous seed title takes the best match by title, year and popularity, and the alternatives are stored so the user can swap with one click. A compile fails only when nothing it needs resolves (for example, a `like_title` prompt whose seed can't be found).
- **Status.** A recipe is `compiling`, `ready`, or `failed` with a reason. A preset keeps its previous recipe until a new compile succeeds. A preset with no ready recipe can't generate.
- **Hand edits.** Hand edits survive until the user explicitly recompiles, the same rule as taste memory (#10). Editing the prompt of a hand-edited recipe asks before discarding the edits.
- **Cost.** At most one compile is queued or running per preset, with a cooldown between compiles. Compile tokens count against the user's monthly budget (ADR 0002 §5).

### 3. Retrieval is a set of sources

Candidates come from several sources, combined into one pool:

- **Recipe queries**: recommendations and similar titles for the seeds, and discover with the recipe's keywords, genres and filters.
- **Taste-seeded recommendations** from the user's top-rated titles of the preset's type.
- **List sources**, selected by role (ADR 0003): `candidates` sources supply titles directly, and `taste` sources contribute their high-rated titles.
- **Cache search**: pgvector nearest neighbours of the taste vector or the mood text over `media_items`, with SQL filters for the hard constraints. Planned, not built. Deferred while the cache is small and biased toward whatever earlier users happened to hydrate.

The prompt type maps to default quotas per source, defined as a rule in the executor. A recipe override replaces the defaults. Each candidate records which sources found it.

### 4. Hard constraints apply to every source

Every candidate must pass the recipe's hard constraints, whichever source found it, including taste-seeded top-ups. If too few titles pass, the row is short rather than filled with off-prompt titles.

### 5. One scorer

Scoring runs after the pool is built, and is the same for every source and both pipelines. It combines:

- **Prompt match**: mood text against the title's embedding, and similarity to the seeds.
- **Taste similarity** against the user's top-rated titles, penalized by anti-taste.
- **Rating confidence**: rating together with vote count.

The prompt type selects a weight set. The embedding model, the embedded text and the weights are not fixed here: #20 decides the model and text, and #11 tunes the weights.

### 6. Pipelines

- **Baseline (#7)** executes the recipe without calling a chat LLM. It runs low-pool refills, and it runs Regenerate until the agent exists.
- **Agent (#8)** runs Regenerate, and later scheduled refreshes. It starts from the recipe and may extend it with its own tool calls. It uses the same scorer to pre-score its pool; its grounding check is unchanged.

### 7. Exhausted presets

If a run adds fewer than a configured number of new titles, the preset is marked **exhausted** and low-pool regeneration stops until its prompt or recipe changes. Manual Regenerate still works.

### 8. Keys and pauses

- **Gate.** A preset prompt and Regenerate need every key the preset's pipelines use: TMDB, the user's LLM key, and the embedding provider's key if #20 keeps an API embedding model. The UI disables the prompt box and Regenerate until those keys are present, and says which one is missing.
- **Pauses are scoped by key.** A 401 or 429 on a key pauses only the work that uses that key. Running out of the monthly token budget pauses only work that spends LLM tokens. Compile jobs and agent runs need the LLM key. Baseline refills need the TMDB key and, if embeddings use an API, that provider's key. This narrows ADR 0002 §5, which pauses all of a user's background runs.

With a local embedding model, a baseline refill uses only TMDB and keeps running while the LLM key is paused. With an API embedding model, refills pause when that key fails. #20 decides which applies.

## Alternatives considered

- **Baseline ignores the prompt** (the plan before this ADR): every baseline row for a user looks alike, and the agent-versus-baseline eval is unfair.
- **The agent interprets the prompt on every run, and the baseline uses only an embedding of the prompt text**: nothing to inspect or edit, every refill costs LLM tokens, and embedding alone misses topic prompts that TMDB keywords answer directly.
- **Numeric quotas and weights written by the LLM**: uncalibrated, and nobody can tune them.
- **Merging hand edits field by field on recompile**: harder for users to predict.
- **Cache-only retrieval**: while the cache is small it would surface the same titles again and again.

## Consequences

- Baseline rows follow the prompt. For baseline refills, only saving or editing a prompt spends LLM tokens.
- Users can see and fix how a prompt was understood. Traces (#9) show the recipe and each candidate's sources.
- A compile step and recipe UI land before the baseline (#7). A preset can't generate until its first compile succeeds.
- Narrow prompts produce short rows, not filler.
- The recipe schema is versioned; changing it means migrating stored recipes or recompiling them.
- The key gate and the pause behaviour of baseline refills depend on #20's embedding decision.
