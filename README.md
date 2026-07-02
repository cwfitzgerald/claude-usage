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
Session                                Model     Src  Date           Input   Output    Cache rd   Cache wr       Total    Cost
-------------------------------------  --------  ---  ----------  --------  -------  ----------  ---------  ----------  ------
PR review helper tool                  opus-4.8  gui  2026-06-15    53,236  190,806  20,270,198    444,956  20,959,196  $19.62
Sync CLAUDE.md and AGENTS.md           gpt-5.5   gui  2026-06-29   124,629   26,911   1,577,600          0   1,729,140   $2.22
...
-------------------------------------  --------  ---  ----------  --------  -------  ----------  ---------  ----------  ------
TOTAL (24 sessions: 18 claude, 6 codex)                          973,735  954,089 103,298,005  2,637,466 107,863,295 $106.05
```

The **Model** column shows the (shortened) model that produced most of the
session's tokens; a trailing `+` marks a session that used more than one model,
and a trailing `(low)` / `(medium)` / `(xhigh)` shows the reasoning effort when
the transcript records one (Codex only — Claude Code doesn't persist it). The
**Date** is the last activity recorded in the transcript.

### Subagents

When a session spawns subagents — which often run a *different* model than the
base conversation — the session is broken out into three kinds of rows: one
**rollup** line for the whole conversation, then the base (`main`) and each
subagent indented beneath it. The rollup's Model is left blank when the base and
subagents didn't all run on the same model:

```
Session                                   Model      Src  Date          Input   Output   Cache rd  Cache wr      Total    Cost
----------------------------------------  ---------  ---  ----------  -------  -------  ---------  --------  ---------  ------
Memory allocator for wgpu-hal                        gui  2026-07-02   21,186   66,973  8,954,027   427,874  9,470,060  $10.86
    main                                  fable-5                       5,855   25,647    367,222    77,819    476,543   $3.26
    Explore: Research VMA algorithms      opus-4.8                      5,066   16,311  3,216,492   124,785  3,362,654   $2.82
    Explore: Research wgpu allocator ...  opus-4.8                      5,219   11,100  3,284,963   113,226  3,414,508   $2.65
    Explore: Research D3D12MA algorithms  opus-4.8                      5,046   13,915  2,085,350   112,044  2,216,355   $2.12
```

Claude subagents are labelled `type: description` from their `.meta.json`
sidecar; Codex subagents are labelled by their `agent_nickname` (an unnamed one,
like an automatic `codex-auto-review` pass, shows as `(subagent)`). Sessions
with no subagents stay as a single flat row. The **TOTAL** row and all sorting
use each conversation's rollup (base + subagents) figure.

`--sort` applies within a conversation too: the subagents are ordered by the
same key (e.g. by cost under `--sort cost`), with `main` always pinned directly
under the rollup line. In `--json`, the top-level numbers are the
whole-conversation rollup, with `base` and a `subagents` array (in spawn order)
broken out alongside.

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

- mark each row `gui` or `cli` in the **Src** column, and
- prefer the app's curated session title.

Point it elsewhere with `--gui-dir`. If no metadata dir exists (CLI-only
machine), every row is shown as `cli` — which is correct, and the cost totals
are unaffected.

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
- **Src column.** Codex records an `originator`; sessions from "Codex Desktop"
  are marked `gui`, the CLI/TUI as `cli`.
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
