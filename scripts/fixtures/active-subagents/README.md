# Active subagents repair fixture

Upload **only** `chat-page.js` and `activity-panel.js` in each fresh Eval session. Use `PROMPT` from `fixture.py`. After the turn, download the saved text by filename and pass it to `await grade(files)` from the same module. Require all checks to pass. The grader runs candidate JavaScript only in the existing isolated worker, with tool dispatch rejected and a five-second timeout. Do not upload the grader, this document, or the Git provenance.

Historical source: `2be46c78aa5142b2220584db51c56ab26d258552` (“Keep active subagents on the latest assistant reply”), parent `85c8509a5e098cf3e8bbeec85a2be97104969509`. The original regression tests are in `apps/frontend/tests/{voice-composer,subagent-inspector}.spec.js` at the fix commit.

The offline proof reconstructs the original and fixed logic from `ChatPage.vue` and `components/AgentActivityPanel.vue` at those exact revisions and checks that the supplied originals match byte for byte. The extraction preserves the historical computed bodies and translates the relevant Vue prop bindings and template branches; DOM, reactivity, unrelated rendering, and batch grouping are outside this reduced fixture. Synthetic IDs and statuses exercise live rows, exact spawn history, and both one-file-only repairs. The older collapsed panel must hide the live spawn row while preserving an ordinary read entry.

```bash
python3 scripts/fixtures/active-subagents/fixture.py /path/to/node
```

Expected: original **3/7**, page-only **3/7**, panel-only **4/7**, fixed **7/7**. This command runs only trusted Git-derived source in Node, never model artifacts. It does not start the application or contact a provider.

## Compare execution in the live development harness

Run this in the isolated worktree stack with the dedicated `script-eval` account. `--model` names an existing configuration used to initialize the evaluator. The control uses a disposable loopback provider; it does not ask that model to generate the patch. It sets the session title before sending the first message so automatic title generation cannot call a separate provider. The backend still reads and edits the uploaded original files through normal session tools and approvals.

```bash
set -o pipefail
python3 scripts/fixtures/active-subagents/fixture.py --fixed-json |
  ./scripts/eval.sh scripted-tools --model halogen-flash-next \
    --known-patch --session-files --skip-safety --cases repo-repair \
    --reasoning-effort model-default --repeats 1 --run-timeout 35
```

Both modes must report `valid`, `byteExact` and `exactOperations` as true and pass all seven behavior checks. To test independent generation, run the same evaluator without `--known-patch` or the input pipe. Add `--repo-full-reads --diagnostics` to check that both complete originals reached the model before the first edit. Diagnostics are for isolated test inputs only; the report emits round numbers, not source text.

Add `--repo-feedback` to test one correction turn in the same session. The evaluator only sends feedback when an in-scope edit was saved and checks still fail. The follow-up carries failed behavior checks with expected and observed values, not source code or the historical patch. Each turn has its own `--run-timeout`; a timed-out first turn can qualify if it saved an edit before cancellation. `initialAttempt` reports the first turn separately when feedback was sent. Passing still requires all seven checks, allowed operations and a completed final turn. An unavailable direct call in script mode is recorded separately; it does not disqualify the run unless it escapes rejection or produces an effect.
