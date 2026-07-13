# claude-usage

A local web dashboard that scans coding-agent session transcripts and reports
token usage and estimated cost. It reads **Claude Code** and
**Codex** sessions, keeps the current result cached in memory, and updates it
only when requested.

> ### 🤖 This is vibe coded
>
> This entire tool — code, docs, and this very sentence — was written by Claude
> (Claude Code) through conversational prompting, with light human steering. It
> has **not** been carefully audited line by line. Treat the numbers as a
> useful approximation, not an invoice. Read the code under
> [`src/claude_usage`](src/claude_usage) before
> trusting it with anything that matters. PRs and fixes welcome.

## Dashboard

```sh
uv sync
uv run claude-usage
```

The server opens <http://127.0.0.1:8765> in your default browser automatically
(`--no-open` disables that for headless use). The initial scan happens in the
background. The dashboard then serves its cached snapshot until **Update data**
is pressed; sorting, searching, and filtering never rescan the files. Expanded
rows show the main agent, nested subagents, model splits, and compaction
segments. Session and subagent ordering are controlled independently; subagent
ordering applies recursively to every nested level.

The server binds only to the loopback interface by default because session
metadata is private and the dashboard has no authentication. Paths can be
overridden with `--projects-dir`, `--gui-dir`, and `--codex-dir`; the bind can
be changed with `--host` and `--port`.

### Terminal compatibility

The previous terminal and JSON reports remain available:

```sh
uv run claude-usage report                 # terminal table
uv run claude-usage report --sort tokens
uv run claude-usage report --since 7d
uv run claude-usage report --json

# Historical option-only invocations also continue to work.
uv run claude-usage --json
```

By default the scanner reads Claude Code transcripts from
`~/.claude/projects/*/*.jsonl` and Codex transcripts from
`~/.codex/sessions/**/rollout-*.jsonl`. Either source may be absent.

### Context semantics

- **Context used** is the sum of the peak context occupancy reached in each
  context lifetime. **Peak context** is the largest single lifetime for that
  agent. A thread's Context value adds its main agent and nested subagents.
  Both tools record compaction boundaries; Claude supplies its pre-compaction
  occupancy directly, while Codex supplies per-request occupancy around the
  boundary. Codex also records the effective model window.

## Terminal report reference

Example:

```
Date        Session                                     Model                Input  Output  Cache rd  Cache wr     Cost
----------  ------------------------------------------  ------------------  ------  ------  --------  --------  -------
2026-06-30  wgpu PR #8388 review feedback               opus-4.8             17.2K  250.9K     52.3M    391.2K   $36.43
2026-06-30  wgpu wiki documentation migration           opus-4.8             63.0K  287.6K     26.3M    514.7K   $25.78
2026-06-29  Claude session token usage analyzer         opus-4.8             42.6K  136.5K     25.1M    611.6K   $22.27
2026-06-29  DX12/Vulkan swapchain synchronization       opus-4.8             12.4K  163.4K     16.6M    541.1K   $17.87
...
----------  ------------------------------------------  ------------------  ------  ------  --------  --------  -------
TOTAL                                                                       511.8K    1.4M    258.9M      4.1M  $223.32
Averages  $18.61 per session (12: 10 claude, 2 codex) · $55.83 per active day (4) · $55.83 per calendar day (4d span)
```

The table sorts by cost (default) and leads with the **Date** of last activity.
A grand-**TOTAL** row sums every token/cost column, and below it an **Averages**
line: cost per session (with the session count and
per-tool breakdown), and two cost-per-day rates — over *active* days (distinct
dates that actually had a session) and over the full *calendar* span (first to
last date, idle days included). The first answers "what a day I use it costs,"
the second is a run-rate; they converge as you narrow the window with `--since`.

Token counts are abbreviated (`53.2K`, `20.3M`) to keep the columns narrow;
`--json` reports the exact integers. On a terminal the table is also colorized
to make it scannable: the **header** is bold, the **Cost** column is tinted by
size (green under \$5, yellow under \$25, red above — a true \$0.00 is dimmed as
"no figure"), and always-zero cells (e.g. Codex's Cache wr) are dimmed. Color is
automatic on a TTY, off when piping, respects `NO_COLOR`, and is forced or
suppressed with `--color always|never`.

The **Model** column shows the (shortened) model that produced most of the
session's tokens; a trailing `+` marks a session that used more than one model
(broken out into per-model rows — see [Multi-model agents](#multi-model-agents)),
and a trailing `(low)` / `(medium)` / `(xhigh)` shows the reasoning effort when
the transcript records one (Codex only — Claude Code doesn't persist it). The
**Date** is the last activity recorded in the transcript.

### Subagents

When a session spawns subagents — which often run a *different* model than the
base conversation — the session is broken out into three kinds of rows: one
**rollup** line for the whole conversation, then the base (`main`) and each
subagent indented beneath it. Subagents can themselves spawn subagents, so the
tree nests to whatever depth the run reached, each agent's children indented
under it. The rollup's Model is left blank when the base and subagents didn't all
run on the same model:

```
Date        Session                                     Model                Input  Output  Cache rd  Cache wr     Cost
----------  ------------------------------------------  ------------------  ------  ------  --------  --------  -------
2026-07-03  Memory allocator for wgpu-hal                                   350.5K  621.1K    374.2M      8.5M  $295.71
            ├─ main                                     fable-5              15.1K  282.5K     20.0M    760.2K   $49.48
            ├─ Chunk C: vulkan allocator port           opus-4.8             23.6K   14.9K     56.0M      1.8M   $39.43
            │  └─ Map current vulkan gpu-allocator ...  haiku-4.5              138   12.6K      1.5M     67.4K    $0.30
            ├─ Chunk D: dx12 allocator port             opus-4.8             48.3K   33.5K     18.7M    226.0K   $11.82
            │  ├─ Explore dx12 integration call sites   haiku-4.5              144    8.7K      2.0M     68.4K    $0.33
            │  └─ Explore D3D12MA allocation policy     haiku-4.5               95     150    656.8K     53.5K    $0.13
            └─ Research VMA algorithms                  opus-4.8              5.1K   16.3K      3.2M    124.8K    $2.82
```

The three row kinds are distinguished two ways. **Tree connectors** (`├─`/`└─`,
with `│` guide lines for deeper nesting; ASCII fallback on legacy consoles) tie
each row to its parent and mark the last child. **Color** (on a terminal, or with
`--color always`) makes it scannable at a glance: the rollup line is **bold**,
`main` is **cyan**, the subagents are **dimmed**, and per-model breakdown rows
(when an agent mixed models) are **dim magenta**; flat single-session rows
keep the default color. The per-cell Cost tint and dimmed zeros described above
apply to these rows too.

Each row shows an agent's *own* usage; a subagent's spawned children are separate
rows below it (just as `main` excludes its subagents), and the top **rollup**
sums the whole tree — base plus every subagent at every depth.

Claude subagents are linked to their parent by the spawning `tool_use` id
recorded in the `.meta.json` sidecar (the layout on disk stays flat — a single
`<session-id>/subagents/` directory — so the tree is reconstructed from that
lineage, not the filesystem). They're labelled by their `description` from the
sidecar (falling back to the agent `type`, e.g. `Explore`, when none was
recorded); Codex subagents link via `parent_thread_id` and are labelled by their
humanized `agent_path` task name, falling back to `agent_nickname` and then
`(subagent)` when unnamed. Sessions with no
subagents stay as a single flat row. The
**TOTAL** row and all sorting use each conversation's rollup (base + subagents)
figure.

`--sort` applies within a conversation too: subagents are ordered by the same key
(e.g. by cost under `--sort cost`) among their siblings at each level, with
`main` always pinned directly under the rollup line. In `--json`, the top-level
numbers are the whole-conversation rollup, with `base` and a `subagents` array
broken out alongside; each subagent carries its own nested `children` array.

### Compaction segments

When a conversation is **compacted** — manually with `/compact` or automatically
when the context window fills — its usage is split into one row per *context
lifetime*. Each `context N` slice is the usage billed while that window was
alive, so the sawtooth is visible: `Cache rd` climbs turn over turn as context
accumulates, then compaction resets it and the next slice starts cheap.

```
Date        Session                                     Model                Input  Output  Cache rd  Cache wr     Cost
----------  ------------------------------------------  ------------------  ------  ------  --------  --------  -------
2026-06-29  Claude session token usage analyzer         opus-4.8             42.6K  136.5K     25.1M    611.6K   $22.27
            ├─ context 1 (→409.1K)                      opus-4.8              6.7K   68.8K     18.2M    382.0K   $14.67
            ├─ context 2 (→134.0K)                      opus-4.8             17.5K   56.0K      5.5M    179.0K    $6.01
            └─ context 3 (live)                         opus-4.8             18.4K   11.7K      1.4M     50.6K    $1.59
```

The `→168K` on a slice is its **peak context occupancy** — how full the window
grew (`preTokens`) right before it rolled over; the final `(live)` slice was
never compacted. That peak is a *different figure* from the token columns, which
sum per-turn billed usage: a slice's `Cache rd` will dwarf its peak, because
every turn re-reads the whole window. The slices always read chronologically
(never reordered by `--sort`), and only appear when a conversation actually
compacted. When a session has subagents too, its segments nest one level deeper,
under `main`. In `--json`, a `segments` array (with `peak_tokens` and `trigger`)
breaks down `base`. Claude supplies an explicit manual/automatic trigger; Codex
records the boundary and context occupancy but not the trigger.

### Multi-model agents

A single agent usually runs one model start to finish, but not always: a stalled
subagent that gets resumed can silently continue on the *session's* model rather
than the tier it was spawned with, and a base conversation can switch models
mid-run. Cost is **always** computed per model — each turn is priced at the rate
of the model that produced it — so the totals are correct regardless. But an
aggregate row can only name one model, hiding the mix, so whenever an agent used
more than one model its own usage is broken out into one indented row per model,
each showing that model's tokens and its own cost. The aggregate row itself
leaves the Model column **blank** — exactly as the whole-conversation rollup does
— since the rows beneath it carry the models:

```
Date        Session                                     Model                Input  Output  Cache rd  Cache wr     Cost
----------  ------------------------------------------  ------------------  ------  ------  --------  --------  -------
2026-07-09  M0 resource table work                                           51.8K  131.8K    117.9M      2.3M   $87.88
            ├─ main                                     fable-5              21.6K   81.0K      2.9M    456.0K   $16.25
            ├─ Implement 0.2 naga front+IR                                    8.9K   19.3K     32.0M    475.4K   $32.21
            │  ├─ fable-5                               fable-5              1.7K   14.3K     21.3M    271.5K   $25.46
            │  └─ opus-4.8                              opus-4.8             7.3K    5.0K     10.6M    203.8K    $6.76
            └─ ...
```

Here the *Implement 0.2* subagent was spawned on opus, stalled, and resumed on
fable — so most of its work (and cost) landed on fable, which the per-model rows
make plain. The breakdown applies to every kind of agent row: the base (`main`),
any subagent, and a compaction segment that itself spanned models each expand the
same way, blanking their own Model cell; a single-model agent stays one row. The breakdown rows are styled dim
magenta to set them apart from subagents (dim) and segments (dim cyan), and are
ordered by the same `--sort` key among themselves. In `--json`, every `base`,
subagent, and segment carries a `per_model` array (one entry per model, with that
slice's tokens and cost); it's always present — a single-element list for a
single-model agent — and its entries sum to that agent's own figure.

## What it does

- Walks every Claude Code transcript (`<project>/<session-id>.jsonl`) and every
  Codex rollout (`sessions/YYYY/MM/DD/rollout-*.jsonl`).
- **Claude:** sums each assistant turn's `message.usage`, deduplicating by API
  message id so resumed/replayed logs aren't double-counted.
- **Subagents:** parses each subagent transcript separately (they often run a
  different model), pricing each at its own rate and showing them indented under
  a whole-conversation rollup line — nested to any depth when a subagent spawns
  its own. Claude stores them flat under `<session-id>/subagents/agent-*.jsonl`
  and links each to its parent by the spawning `tool_use` id in the sidecar;
  Codex writes each as its own top-level `rollout-*.jsonl` that links back via
  `parent_thread_id`. Either way the tool reconstructs the tree and folds it into
  the parent.
- **Compaction:** splits a Claude conversation at each `compact_boundary` into
  per-context-lifetime rows, tagged with the peak occupancy it rolled over at.
- **Codex:** reads the cumulative `total_token_usage` from the session's last
  `token_count` event — no dedup needed.
- Splits tokens into **input**, **output**, **cache read**, and **cache write**
  (Codex has no cache-write concept, so that column is always 0 for it).
- Names each session by its tool's title (Claude's `ai-title`, Codex's
  `session_index.jsonl` thread name), falling back to a summary / first prompt,
  then `(untitled)`.
- Prices each session against the model that produced it.

## Why these numbers are *lower* than the desktop app's "Breakdown"

If you compare a session here against the **Breakdown** panel in the desktop
app, this tool will report fewer tokens — often roughly half to a third. **This
tool is the accurate one; the app's panel over-counts.**

Here's why. A single assistant API response is written to the transcript as
*several* JSONL lines — one per content block (a `thinking` block, a `text`
block, each `tool_use` block) — and **every one of those lines repeats the same
`usage` object**. One response with `[thinking, text, tool_use, tool_use]`
becomes four lines, all carrying identical `input` / `output` / `cache_read` /
`cache_write` counts and the same `requestId`. It was billed *once*.

This tool dedupes by API `message.id`, so it counts that response once. The
desktop app's Breakdown panel appears to tally the blocks separately, inflating
every multi-block turn by its block count (~2–3× in practice). Anthropic bills
per API call, so the deduped figure is the one that matches real cost.

Across a full set of transcripts here, 575 of 742 unique message ids spanned
multiple lines, and **none** of them carried differing usage between those lines
— confirming the duplicates are always the same response re-rendered, never
distinct billed calls. Deduping never drops real tokens.

## GUI vs CLI sessions

The desktop app ("Cowork" / Claude Code in the GUI) doesn't keep separate token
data — it runs the bundled CLI, so its transcripts land in the **same**
`~/.claude/projects/` directory and are already counted. The app only adds a
metadata layer (curated title, cwd, model), keyed by the CLI session id, in the
desktop app's data dir. The tool checks the known locations:

- `%APPDATA%\Claude\claude-code-sessions` — normal Windows install.
- `%LOCALAPPDATA%\Packages\Claude*\LocalCache\Roaming\Claude\claude-code-sessions`
  — **packaged (MSIX/Microsoft Store) install**, where Windows redirects the
  app's `%APPDATA%` into its package container.
- `~/Library/Application Support/Claude/claude-code-sessions` — macOS.

It uses the first that exists to:

- tag each session `gui` or `cli` (surfaced as `source` in `--json`), and
- prefer the app's curated session title.

Point it elsewhere with `--gui-dir`. If no metadata dir exists (CLI-only
machine), every session is treated as `cli` — which is correct, and the cost
figures are unaffected.

## Codex sessions

Codex transcripts live under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. A
few things differ from Claude:

- **Usage is read, not summed.** Codex writes periodic `token_count` events
  whose `total_token_usage` is *cumulative*, so the tool takes the final running
  total. No per-turn summing or message-id dedup. At compaction boundaries,
  adjacent cumulative snapshots are subtracted to produce context slices.
- **Cached input is part of input.** Codex's `input_tokens` already *includes*
  the cached prompt tokens, so the tool splits them out: the cached slice goes
  in the **Cache rd** column (priced at the discounted rate), the rest in
  **Input**. `output_tokens` already includes reasoning tokens.
- **Source tag.** Codex records an `originator`; sessions from "Codex Desktop"
  are tagged `gui`, the CLI/TUI as `cli` (surfaced as `source` in `--json`).
- **Naming.** Recent sessions are named from `~/.codex/session_index.jsonl`;
  older ones fall back to their first real user prompt.
- **Reasoning effort.** Codex records `reasoning_effort` per turn (under
  `turn_context.collaboration_mode.settings`); the last non-null value is shown
  as a `(low)` / `(medium)` / `(xhigh)` suffix on the Model column. Older
  sessions that didn't record it show no suffix.
- **Compaction.** A `compacted` record starts a new context lifetime. The tool
  uses `last_token_usage` to retain each lifetime's peak occupancy while
  preserving the unchanged cumulative billed total.
- **Subagents.** Codex writes each subagent as its own `rollout-*.jsonl` with a
  `parent_thread_id` in its `session_meta`; the tool folds these into the parent
  and shows them indented (see [Subagents](#subagents) above). The automatic
  `codex-auto-review` pass is written the same way, but it's a machine-internal,
  unpriced pass, so its rollouts are **dropped entirely** rather than shown as
  $0.00 rows.
- A single rollout that switches models mid-session is still priced against its
  dominant model, since the cumulative total isn't split per model.

**pi is not supported yet.** [`pi`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
keeps only config under `~/.pi/`; no session transcripts were found on disk to
read. If you know where pi persists sessions, a parser would be welcome.

## Cost model

Cost is an estimate based on published per-MTok rates (`PRICING` in
[`src/claude_usage/core.py`](src/claude_usage/core.py)), with the standard cache multipliers applied to the
input rate:

| Token type            | Multiplier vs input rate |
| --------------------- | ------------------------ |
| Cache read            | 0.1×                     |
| Cache write (5-min)   | 1.25×                    |
| Cache write (1-hour)  | 2.0×                     |

`PRICING` covers the full current Claude line-up — Fable 5, Mythos 5, the Opus
4.x, Sonnet (5, 4.6, 4.5, 4), and Haiku (4.5, 3.5) families. Sonnet 5 has a dated
price bump ($2/$10 per MTok through 2026-08-31, then $3/$15); it's priced at the
higher, going-forward rate. A dated model id like `claude-haiku-4-5-20251001`
matches its base entry, and a bare family alias with no version (e.g. `opus` or
`fable`, which the logs sometimes record instead of the resolved id) is priced at
the latest version of that family.

The same formula covers OpenAI/Codex models. OpenAI cached input is 10% of the
uncached input rate. GPT-5.6 cache writes are 1.25× input if the transcript
reports them. Priced Codex models include GPT-5.6 Sol, Terra, and Luna (plus the
unsuffixed Sol alias), GPT-5.5 and GPT-5.5 Pro, and GPT-5.4, mini, nano, and Pro.
These are standard-tier list prices as of 2026-07; priority processing,
regional uplifts, Batch/Flex discounts, and long-context surcharges are not
modelled. Other Codex models seen in the wild (for example, a locally served
Qwen) have no entry. The internal `codex-auto-review` model is not priced
either; its rollouts are dropped upstream instead of producing a warning (see
[Codex sessions](#codex-sessions)).

Models without a pricing entry are reported with `$0.00` and a stderr warning;
add them to `PRICING` to fix. These are **list prices** and don't reflect any
subscription/plan billing — treat the totals as an approximation, not a bill.

## License

Licensed under any of:

- [MIT license](LICENSE-MIT)
- [Apache License, Version 2.0](LICENSE-APACHE)
- [zlib license](LICENSE-ZLIB)

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted
for inclusion in this project by you shall be licensed as above, without any
additional terms or conditions.
