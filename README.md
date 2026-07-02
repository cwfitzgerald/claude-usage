# claude-usage

A small tool that scans your local coding-agent session transcripts and reports
token usage and estimated cost per session. It reads **Claude Code** and
**Codex** sessions and shows them side by side in one table, each row labelled
with the model that produced it.

> ### 🤖 This is vibe coded
>
> This entire tool — code, docs, and this very sentence — was written by Claude
> (Claude Code) through conversational prompting, with light human steering. It
> has **not** been carefully audited line by line. Treat the numbers as a
> useful approximation, not an invoice. Read [`usage.py`](usage.py) before
> trusting it with anything that matters. PRs and fixes welcome.

## Usage

```sh
python usage.py                 # table, sorted by cost (default)
python usage.py --sort tokens   # sort by total tokens
python usage.py --sort name     # sort alphabetically
python usage.py --sort date     # sort by last-activity date, newest first
python usage.py --since 7d      # only sessions active in the last 7 days
python usage.py --since 24h     # ...the last 24 hours (units: h/d/w/mo)
python usage.py --since 3mo     # ...the last 3 months
python usage.py --since 2026-06-01  # ...on or after an absolute date
python usage.py --color always  # force color (default: auto — on for a TTY)
python usage.py --json          # machine-readable JSON
python usage.py --projects-dir /path/to/.claude/projects   # Claude transcripts
python usage.py --codex-dir /path/to/.codex/sessions       # Codex transcripts
```

By default it reads Claude Code transcripts from `~/.claude/projects/*/*.jsonl`
and Codex transcripts from `~/.codex/sessions/**/rollout-*.jsonl`. Either source
is optional — missing directories are simply skipped. No dependencies beyond a
recent Python 3 (3.10+).

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
session's tokens; a trailing `+` marks a session that used more than one model,
and a trailing `(low)` / `(medium)` / `(xhigh)` shows the reasoning effort when
the transcript records one (Codex only — Claude Code doesn't persist it). A few
unwieldy ids get an explicit short alias (e.g. `codex-auto-review` shows as
`cdx-ar`). The **Date** is the last activity recorded in the transcript.

### Subagents

When a session spawns subagents — which often run a *different* model than the
base conversation — the session is broken out into three kinds of rows: one
**rollup** line for the whole conversation, then the base (`main`) and each
subagent indented beneath it. The rollup's Model is left blank when the base and
subagents didn't all run on the same model:

```
Date        Session                                     Model                Input  Output  Cache rd  Cache wr     Cost
----------  ------------------------------------------  ------------------  ------  ------  --------  --------  -------
2026-07-02  Memory allocator for wgpu-hal                                    79.9K  175.2K     92.6M      2.0M   $70.26
            ├─ main                                     fable-5               6.4K   65.2K      2.2M    278.3K   $11.13
            ├─ Fix soundness findings in allocator      opus-4.8             17.4K   21.8K     34.6M    273.4K   $19.62
            ├─ Research VMA algorithms                  opus-4.8              5.1K   16.3K      3.2M    124.8K    $2.82
            └─ Re-verify soundness fixes                opus-4.8              5.8K      28    323.9K    108.7K    $0.87
```

The three row kinds are distinguished two ways. **Tree connectors** (`├─`/`└─`,
ASCII fallback on legacy consoles) tie each base/subagent row to its rollup and
mark the last child. **Color** (on a terminal, or with `--color always`) makes
it scannable at a glance: the rollup line is **bold**, `main` is **cyan**, and
the subagents are **dimmed**; flat single-session rows keep the default color.
The per-cell Cost tint and dimmed zeros described above apply to these rows too.

Claude subagents are labelled by their `description` from the `.meta.json`
sidecar (falling back to the agent `type`, e.g. `Explore`, when none was
recorded); Codex subagents are labelled by their `agent_nickname` (an unnamed
one, like an automatic `codex-auto-review` pass, shows as `(subagent)`).
Sessions with no subagents stay as a single flat row. The **TOTAL** row and all
sorting use each conversation's rollup (base + subagents) figure.

`--sort` applies within a conversation too: the subagents are ordered by the
same key (e.g. by cost under `--sort cost`), with `main` always pinned directly
under the rollup line. In `--json`, the top-level numbers are the
whole-conversation rollup, with `base` and a `subagents` array (in spawn order)
broken out alongside.

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
breaks down `base`; only Claude Code records compaction, so Codex sessions have
none.

## What it does

- Walks every Claude Code transcript (`<project>/<session-id>.jsonl`) and every
  Codex rollout (`sessions/YYYY/MM/DD/rollout-*.jsonl`).
- **Claude:** sums each assistant turn's `message.usage`, deduplicating by API
  message id so resumed/replayed logs aren't double-counted.
- **Subagents:** parses each subagent transcript separately (they often run a
  different model), pricing each at its own rate and showing them indented under
  a whole-conversation rollup line. Claude stores them under
  `<session-id>/subagents/agent-*.jsonl`; Codex writes each as its own top-level
  `rollout-*.jsonl` that links back via `parent_thread_id`, which the tool folds
  into the parent.
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
  whose `total_token_usage` is *cumulative*, so the tool just takes the final
  running total. No per-turn summing or message-id dedup.
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
- **Subagents.** Codex writes each subagent (including an automatic
  `codex-auto-review` pass) as its own `rollout-*.jsonl` with a
  `parent_thread_id` in its `session_meta`; the tool folds these into the parent
  and shows them indented (see [Subagents](#subagents) above).
- A single rollout that switches models mid-session is still priced against its
  dominant model, since the cumulative total isn't split per model.

**pi is not supported yet.** [`pi`](https://www.npmjs.com/package/@earendil-works/pi-coding-agent)
keeps only config under `~/.pi/`; no session transcripts were found on disk to
read. If you know where pi persists sessions, a parser would be welcome.

## Cost model

Cost is an estimate based on published per-MTok rates (`PRICING` in
[`usage.py`](usage.py)), with the standard cache multipliers applied to the
input rate:

| Token type            | Multiplier vs input rate |
| --------------------- | ------------------------ |
| Cache read            | 0.1×                     |
| Cache write (5-min)   | 1.25×                    |
| Cache write (1-hour)  | 2.0×                     |

The same formula covers OpenAI/Codex models: OpenAI's cached-input rate is 10%
of the input rate (the same 0.1× as cache read), and there's no cache-write
surcharge, so those buckets stay zero. Priced Codex models: `gpt-5.5`,
`gpt-5.5-pro`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.4-nano`, `gpt-5.4-pro`
(standard-tier list prices, USD/MTok, as of 2026-07; the pricier "priority" tier
isn't modelled). Other Codex models seen in the wild (e.g. `codex-auto-review`,
locally-served Qwen) have no entry.

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
