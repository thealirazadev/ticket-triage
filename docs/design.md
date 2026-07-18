# ticket-triage - Design

ticket-triage has no graphical UI (a helpdesk frontend is an explicit non-goal), so this document covers the two surfaces humans actually look at: the eval runner's terminal output and the JSON API/log conventions. Visual-design sections (color themes, typography, spacing scales) do not apply and are replaced by the terminal-UX equivalents below.

## Eval runner terminal UX (`python -m evals.run`)

### Output layout

Plain text to stdout, three sections, readable when piped to a file:

```
ticket-triage eval
  dataset   evals/dataset.jsonl  (64 rows, sha256 1f3a…9c)
  baseline  evals/baseline.json  (model example-model-id, prompt v1, 2026-07-20)
  model     example-model-id     prompt v1

intent      accuracy 0.88  macro-F1 0.85   (baseline 0.89 / 0.86)
  label             prec   rec    f1     n
  billing           0.92   0.85   0.88   13
  bug               0.83   0.91   0.87   11
  ...
  confusion (top): refund -> billing x3, how_to -> other x2

priority    accuracy 0.81  macro-F1 0.78   (baseline 0.80 / 0.77)
  ...

sentiment   accuracy 0.94  macro-F1 0.93   (baseline 0.94 / 0.92)
  ...

parse failures: 0 of 64   provider errors: 0
cost: 63,410 in / 4,988 out tokens   $0.097   elapsed 2m 04s

verdict: PASS  (threshold 0.02; largest drop: intent accuracy -0.01)
```

- Aligned fixed-width columns, no table-drawing characters, no progress spinners (a simple `n/total` counter line updated per row, suppressed when stdout is not a TTY).
- The verdict line is always last, always starts with `verdict:`, and always states the threshold and the largest observed drop, on PASS as much as on FAIL - the number a reviewer needs is never hidden.
- `--update-baseline` prints the old and new metric values and the written path.

### Color

- ANSI color only when stdout is a TTY and `NO_COLOR` is unset: green `PASS`, red `FAIL`, yellow for warnings (e.g. `--limit` runs where the gate is skipped) and for metric drops within threshold.
- Color is never the only signal: the words `PASS`/`FAIL`/`warning:` carry the meaning; piped output is byte-identical minus escape codes.

### Verbosity

- Default: the layout above.
- `-v`: adds one line per mismatched row (`eval-0031  intent gold=refund pred=billing`), and per-row provider retries.
- `-q`: header and verdict line only.

### Errors and exit codes

- Errors go to stderr as `error: <one clear sentence>` plus, where useful, one line of remedy (`set LLM_API_KEY in .env or the environment`). Never a traceback for expected failures.
- Exit codes: `0` pass (or gate not applicable), `1` regression gate failed, `2` configuration or runtime error (missing key, unreadable dataset, dataset/baseline hash mismatch).

## API response design

- JSON only; snake_case field names; ISO-8601 UTC timestamps; nulls are explicit (`triage: null`), never omitted keys, so clients need no existence checks.
- One list envelope everywhere: `{"<items>": [...], "total", "limit", "offset"}`.
- One error shape everywhere: `{"error": {"code", "message"}}`. `message` is a complete, friendly sentence; `code` is the machine contract.
- State is explicit, not inferred: `status`, `queue`, and `triage_error` tell a reviewer exactly why a ticket is where it is without reading logs.
- The API never returns HTML and never embeds user content in messages. Ticket text and model summaries are data fields; any client rendering them must escape as plain text.
- Empty states are well-formed successes: empty lists, zeroed stats, empty export stream - never errors.

## Log line design

One JSON object per line: `timestamp`, `level`, `logger`, `message`, then context fields (`request_id`, `route`, `status_code`, `duration_ms`, `ticket_id`, `error_code`, `llm_outcome`, `circuit_state`). Messages are short and grep-stable (`ticket triaged`, `circuit opened`); variability lives in the fields. No ticket content, no secrets, no multi-line entries.

## Accessibility baseline

With no GUI, accessibility obligations land on the terminal and API surfaces: no color-only meaning and `NO_COLOR` respect (above); report layout readable by screen readers (linear, labeled, no ASCII art); API and OpenAPI descriptions in plain language. If any web frontend is ever proposed, it starts from semantic HTML, labeled inputs, keyboard operability, visible focus, and WCAG AA contrast - and from a new phase, not this build.
