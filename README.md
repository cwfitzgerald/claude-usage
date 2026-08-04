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

### Dashboard data

By default the scanner reads Claude Code transcripts from
`~/.claude/projects/*/*.jsonl` and Codex transcripts from
`~/.codex/sessions/**/rollout-*.jsonl`. Either source may be absent.

The summary table shows each conversation's last activity, total context used,
dominant model, token counts, and estimated cost. Expand a conversation to see
the base agent, nested subagents, model splits, and compaction segments.

- **Context used** is the sum of the peak context occupancy reached in each
  context lifetime. **Peak context** is the largest single lifetime for that
  agent. A conversation's Context value includes its base agent and all nested
  subagents.
- **Subagent rows** contain that agent's own usage. A conversation's summary
  rolls up the base agent and every subagent, including nested descendants.
- **Compaction segments** are chronological context lifetimes. Their context
  value is occupancy at the boundary, while their token columns sum billed
  usage across requests and can therefore be much larger.
- **Model splits** appear when one agent used more than one model. Each slice is
  priced independently at that model's rate.
- A **⚡ badge** marks usage billed on Codex's fast (priority) tier.

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
- Names each session by its tool's title (Claude's `custom-title`, Codex's
  `session_index.jsonl` thread name), falling back to the session's first prompt,
  then `(untitled)`. The desktop app's curated title wins over both when present.
  Claude's older `ai-title` and `summary` records are still read, so transcripts
  written by earlier versions keep their names.
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

## Claude Desktop metadata

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

It uses the first that exists to prefer the app's curated session title. Point
it elsewhere with `--gui-dir`. If no metadata directory exists, the transcript
title is used instead and the cost figures are unaffected.

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
- **Naming.** Recent sessions are named from `~/.codex/session_index.jsonl`;
  older ones fall back to their first real user prompt.
- **Fast mode.** See [Fast mode](#fast-mode) below — it changes what a session
  costs, not just how it's labelled.
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
These are standard-tier list prices as of 2026-07; priority processing is
modelled (see [Fast mode](#fast-mode)), while regional uplifts, Batch/Flex
discounts, and long-context surcharges are not. Other Codex models seen in the
wild (for example, a locally served Qwen) have no entry. The internal
`codex-auto-review` model is not priced either; its rollouts are dropped upstream
instead of producing a warning (see [Codex sessions](#codex-sessions)).

Models without a pricing entry are shown with `$0.00` and a scan warning in the
dashboard; add them to `PRICING` to fix. These are **list prices** and don't
reflect any subscription/plan billing — treat the totals as an approximation,
not a bill.

### Fast mode

Codex can run a thread on OpenAI's **priority** service tier, which its own model
metadata calls *Fast* ("1.5× speed, increased usage"); it's enabled per-thread or
globally via `service_tier = "priority"` in `~/.codex/config.toml`. Priority bills
at a flat per-model multiple of the standard rate, applied alike to input, cached
input, and output — so the cache multipliers above still hold and one factor per
model is enough (`PRIORITY_MULT` in
[`src/claude_usage/core.py`](src/claude_usage/core.py)):

| Model                              | Priority vs standard |
| ---------------------------------- | -------------------- |
| GPT-5.6 Sol / Terra / Luna, GPT-5.4 | 2×                  |
| GPT-5.5                            | 2.5×                 |
| GPT-5.4 nano                       | n/a — standard only  |

Anything unlisted falls back to 2×. The tier comes from Codex's
`thread_settings_applied` event (`thread_settings.service_tier`). A rollout that
never records one — anything before Codex began writing the event — is priced at
the standard rate, as is an explicit `default`.

**A thread can toggle fast mode mid-run, so the tier is applied per turn, not per
session.** Fast mode bills a whole stream, and no stream is ever split: Codex
writes `thread_settings_applied` *before* the turn it governs (in the logs it is
followed by that turn's `turn_context` within milliseconds), so a toggle always
lands between turns. Each turn therefore bills entirely on whatever tier was in
effect when it ran, and a change never applies retroactively to turns already
completed.

Codex's token counter is cumulative, so each turn's billed usage is taken as the
counter's advance since the previous snapshot and booked against that turn's
tier. Summing those per-turn deltas reproduces the same totals as reading the
final counter, so the token columns are unaffected — only the cost splits. This
works at every level: a context slice that spans a toggle splits too, so ⚡ can
appear on one slice of a session and not the next.

A ⚡ in the Model column means some of that row's tokens billed at the fast
rate. The badge is based on billed usage rather than the thread's final setting,
so it remains accurate when fast mode was switched off part-way through.

A forked subagent's rollout opens with a verbatim replay of its parent's history,
including the parent's settings, so its starting tier is taken from the last
settings record *before* the inter-agent trigger that ends the replay — the
child's own. In practice a child inherits its parent's tier, but nothing
guarantees it, and each subagent is priced on the tiers it actually recorded.

**Claude Code has no equivalent marker.** Its transcripts report
`message.usage.service_tier`, but it reads `standard` on every turn regardless of
whether fast mode was on, so there is nothing to key a Claude-side adjustment
off.

## License

Licensed under any of:

- [MIT license](LICENSE-MIT)
- [Apache License, Version 2.0](LICENSE-APACHE)
- [zlib license](LICENSE-ZLIB)

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted
for inclusion in this project by you shall be licensed as above, without any
additional terms or conditions.
