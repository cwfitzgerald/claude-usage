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
session's tokens; a trailing `+` marks a session that used more than one model.
The **Date** is the last activity recorded in the transcript.

## What it does

- Walks every Claude Code transcript (`<project>/<session-id>.jsonl`) and every
  Codex rollout (`sessions/YYYY/MM/DD/rollout-*.jsonl`).
- **Claude:** sums each assistant turn's `message.usage`, deduplicating by API
  message id so resumed/replayed logs aren't double-counted.
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
- A session that mixes models (e.g. an internal `codex-auto-review` pass) is
  priced against its dominant model, since the cumulative total isn't split
  per model.

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
`gpt-5.4` (list prices, USD/MTok, as of 2026-06). Other Codex models seen in
the wild (e.g. `codex-auto-review`, locally-served Qwen) have no entry.

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
