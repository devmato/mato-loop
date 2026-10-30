# Session cache evaluation — 2026-10-30

## Outcome

Ordinary follow-ups and session switches reuse the cache successfully. A real
file-read workflow reproduces cache loss at the next user turn: persisted tool
arguments have a different JSON key order, and the adapter turns that into a
different model-facing argument string. Deterministic argument serialization is
the smallest fix: both provider conversions now sort argument keys. The same
failure occurred with memory disabled, so disabling memory is not a solution.

The fixed live experiment preserves all four measured message prefixes without
offline normalization. The first signal of the second turn improves from 19.591 s
to 1.430 s with memory, and from 18.868 s to 0.789 s without memory.

All 28 measured user turns, including six after the fix, returned the expected
answers. Synthetic sessions and temporary login sessions were cleaned up without
reported failures.

## Scope

Investigate slow follow-up messages in session
`55ce8b11-08e2-5efb-9e6c-8d150222d26d`, especially the transition from live
tool-call history to persisted history. The initial evaluation did not change
runtime code. The subsequent fix changes only argument serialization; provider
configuration, permissions, and production data remain unchanged.

All live experiments use real development sessions through the HTTP session API,
the normal MatoLoop agent loop, PostgreSQL persistence, and the configured
`halogen-qwen3.8-flash-next` model through the `openai-compatible` adapter.
Trust is private and pseudonymization is disabled. The baseline uses the default
general session template; the file-read experiment uses the Coding template and
Plan agent, matching the reported workflow more closely.

The running development backend checkout was `e1e8759d`; the evaluator checkout
was `fc89ea36`. Their provider adapter is identical, and the only agent-code
difference is presentation of repairable tool-argument failures. No runtime source
was changed for the before-fix measurements. The production diagnostic build was
`d63084f8`, so development results are not a replay of that exact deployment.

## Reported session

| Request | Input tokens | Cached tokens | Raw cache hit rate | First model signal | Provider duration |
| --- | ---: | ---: | ---: | ---: | ---: |
| First turn, final model request | 60,802 | 58,621 | 96.4% | 1.450 s | 43.794 s |
| Second turn, first model request | 65,876 | 12,480 | 18.9% | 26.629 s | 40.955 s |

Context preparation for the second turn took 2.684 s. The larger delay therefore
occurs after dispatch to the provider. A high cache hit rate does not eliminate
reasoning or output-generation time: the first row still spent 43.794 s generating
2,497 output tokens, including 1,111 reasoning tokens.

The recorded tool schemas and prior message contents were unchanged across this
turn boundary, apart from tool display metadata that the provider adapter omits.
These diagnostics are stored as JSONB and cannot establish the original ordering
of nested argument keys.

## Baseline: ordinary follow-ups and session switches

Command:

```bash
./scripts/eval.sh session-cache \
  --adapter openai-compatible --model halogen-qwen3.8-flash-next \
  --runs 2 --run-timeout 300 \
  --output /workspace/data/cache-eval-2026-10-30-baseline.json \
  --report /workspace/data/cache-eval-2026-10-30-baseline.md
```

Each replicate runs `A1 → A2 → B1 → A3 → C1 → B2 → A4 → C2` in three
independent sessions. Prompts request short session identifiers without tool calls.
Memory remains enabled. A warm-up and automatically generated titles also call
the provider but are not included in the 16 measured turns.

| Phase | Correct answers | Estimated reusable-prefix coverage, median | First model signal, median | Turn duration, median |
| --- | ---: | ---: | ---: | ---: |
| Immediate follow-up | 2/2 | 99.8% | 1.030 s | 5.307 s |
| Return after another session | 8/8 | 99.7% | 1.088 s | 4.242 s |

All 16 measured answers were correct. Both replicates showed no cache-loss signal
under the evaluator's existing thresholds. Total evaluator duration was 191.886 s;
measured turn durations sum to 73.610 s. The difference includes warm-up and title
requests and must not be attributed to chat latency.

Reusable-prefix coverage here is an estimate from the preceding input/output
token counts, not an exact comparison of rendered provider tokens. No cache-write
metric was returned. Session switching alone did not reproduce the reported delay.

## PostgreSQL argument-order probe

A read-only `SELECT CAST(:value AS jsonb)` against the development database,
followed by the real adapter's `_chat_messages`, produced:

```json
{"root": "computer", "path": ".", "pattern": "**/*.py"}
{"path": ".", "root": "computer", "pattern": "**/*.py"}
```

The dictionaries compare equal, but their serialized tool-argument strings do not.
Canonical serialization with sorted keys produces equal strings in this probe.
The probe asserts both semantic equality and differing current wire strings.

Relevant code paths:

- `agent/model_turn.py`: retains live tool calls in a run-local map.
- `agent/projection.py`: projects live calls while that map exists; later runs use
  persisted calls instead.
- `models/session.py`: stores `Message.tool_calls` as PostgreSQL JSONB.
- `providers/openai_compatible.py`: serializes function arguments without sorted
  keys in both Chat Completions and Responses input conversion.

This establishes a local prefix-instability mechanism, independent of provider
queueing or cache routing. Its effect on the live file-read experiment is reported
below.

## Live file-read experiment

The temporary probe in `data/cache-tool-session-eval.py` creates independent Plan
sessions with memory enabled and disabled. It selects the existing connected
development execution agent but reads only synthetic files in each session's
internal storage. Each session has three user turns:

1. Read `probe-a.txt` and return its final verification code.
2. Read `probe-b.txt` and return its final verification code.
3. Return both previously read codes without another tool call.

Each file contains 420 synthetic checksum records and a final code. This creates
a substantial retained tool result without reading a real repository or user file.
Model calls and tools run through the normal HTTP session API. The probe records
per-model-call usage and timings, captures live tool arguments from SSE, and
compares them with the persisted messages.

Provider-message comparisons are reconstructed using the recorded model request
and the argument-key order observed in live SSE. They are not network packet
captures. Serialized argument strings are preserved as strings in the probe's
output, so JSONB cannot erase their ordering difference during this comparison.

The first memory-enabled follow-up reproduced the cache loss:

- Previous final request: 48,001 input tokens.
- Follow-up first request: 49,870 input tokens; only 14,080 cached, or 28.2%.
- First signal: 19.591 s; approximately 35,790 input tokens were not cached.
- The earliest changed message was the first assistant tool call, at zero-based
  message index 4. Its content, call ID, function name, argument values, and
  reasoning text were unchanged. Only the argument-string key order changed:

```json
{"root": "files", "path": "probe-a.txt", "limit": 1000}
{"path": "probe-a.txt", "root": "files", "limit": 1000}
```

The same SSE-to-storage comparison found reordered keys in the second file-read
call as well. The model chose the original order itself; the probe did not force a
particular JSON key order.

The complete boundary results are:

| Memory | User turn | Input tokens | Cached tokens | Raw cache hit rate | First model signal |
| --- | ---: | ---: | ---: | ---: | ---: |
| Full | 2 | 49,870 | 14,080 | 28.2% | 19.591 s |
| Full | 3 | 85,368 | 49,856 | 58.4% | 20.505 s |
| Disabled | 2 | 46,587 | 11,968 | 25.7% | 18.868 s |
| Disabled | 3 | 81,098 | 46,528 | 57.4% | 19.493 s |

These are the first model requests of the respective user turns, not the entire
turn duration. Turn 2 also reads a new large file; processing that new file is
expected work. Turn 3 performs no new tool call, yet still waits about 20 s for the
first signal because the preceding tool call is now serialized differently.

All six answers were correct across ten model requests and four successful read
calls. Measured turn durations sum to 187.189 s. All four live read calls had equal
argument values but unequal serialized arguments after persistence. Disabling
memory did not remove the mismatch or its associated wait.

An offline comparison of the complete retained provider-message prefix found only
the tool-argument serialization changed at all four measured boundaries.
Re-serializing those argument strings with sorted keys made the compared prefixes
identical in all four cases, verified by executable assertions, including both
memory-disabled follow-ups. This checks the proposed mechanism without modifying
or replaying live provider requests.

Local raw artifacts, intentionally outside version control:

- `data/cache-eval-2026-10-30-baseline.json`
- `data/cache-eval-2026-10-30-baseline.md`
- `data/cache-eval-2026-10-30-tools.json`
- `data/cache-eval-2026-10-30-tools-summary.json`
- `data/cache-eval-2026-10-30-tools-fixed.json`
- `data/cache-eval-2026-10-30-comparison.json`
- `data/cache-tool-session-eval.py` (temporary supplemental evaluator)

## Implemented fix

Both `_chat_messages` and `_responses_input` in
`apps/backend/src/mato_loop_backend/providers/openai_compatible.py` now pass
`sort_keys=True` when serializing function arguments. This is a two-line runtime
change. Tool lists, argument values, and privacy-preserving history projection are
unchanged; no migration or dependency is needed.

One regression case in `apps/backend/tests/test_provider_conversion.py`,
parameterized over both conversions, verifies that equal argument dictionaries
with different insertion orders produce identical provider output.

Validation:

```bash
./scripts/dev.sh test backend tests/test_provider_conversion.py \
  tests/test_provider_reasoning.py tests/test_provider_service.py
./scripts/dev.sh check backend
```

All 214 selected tests passed, and the backend check passed. For the live
comparison, the development backend was recreated from the fixed `fc89ea36`
worktree, with its existing data volumes preserved and no active runs at restart.

## Post-fix live comparison

The same temporary evaluator, prompts, fixture sizes, model, and memory modes are
used again; only its output filename changes to preserve the before-fix data.
Each run creates fresh sessions and synthetic checksums rather than reusing the
previous test files.

| Memory | User turn | Raw cache hit rate, before → after | First model signal, before → after | Complete turn, before → after |
| --- | ---: | ---: | ---: | ---: |
| Full | 2 | 28.2% → 96.0% | 19.591 s → 1.430 s | 42.942 s → 24.711 s |
| Full | 3 | 58.4% → 97.9% | 20.505 s → 1.379 s | 24.149 s → 3.170 s |
| Disabled | 2 | 25.7% → 98.3% | 18.868 s → 0.789 s | 41.739 s → 24.913 s |
| Disabled | 3 | 57.4% → 99.1% | 19.493 s → 0.790 s | 22.882 s → 3.430 s |

With memory enabled, the second turn's first model signal fell from 19.591 s to
1.430 s. The third turn, which requires no new tool call, completed in 3.170 s
instead of 24.149 s. Reading a new large file still takes time: the second turn's
second model request processes the newly read file, so that turn's total duration
fell from 42.942 s to 24.711 s rather than to approximately one second.

All six post-fix answers were correct, again using ten model requests. Executable
assertions confirm that all four retained provider-message prefixes are identical
across their turn boundary without offline re-serialization. No cleanup failures
were reported. Total measured turn time fell from 187.189 s to 113.684 s; this
includes initial processing of new large file results, not just follow-up latency.

## Limitations

- The original production run was inspected read-only, not replayed or modified.
- The synthetic file workflow is narrower than a complete repository review.
- Provider queueing, cache eviction, routing, and output generation can still add
  latency after local prefix instability is fixed.
- The live comparison is a small paired workflow experiment, not a statistical
  performance benchmark or a guaranteed provider-latency bound.
- Ordinary-session baseline coverage is a token-count estimate, not a tokenizer
  or cache-internals trace.
- The selected provider tests and backend check were run locally; the complete
  backend test suite and CI were not run.
- Early probe setup attempts were rejected for missing explicit null content,
  selecting Plan outside the Coding template, and omitting the required execution
  agent. These are excluded from the cache measurements; none reached the model.
