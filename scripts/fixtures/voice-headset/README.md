# Held-out voice-headset repair

The evaluator uploads only `voice-headset.js` in a fresh session and uses the same prompt in all tool modes. It grades the saved file in the bounded JS worker with no tool dispatch. This is a reduced UI logic fixture: it tests the visibility decision, not Vue reactivity, DOM rendering, or voice connections.

The original is extracted from `ChatComposer.vue` at `7d823ea7eb033c7438b962a1d14b7329a5073c21`; the historical fix is `30c5e335c3d64955ca21ec73644da825a8ce2c28`. `historical_source(revision)` reconstructs either version for offline validation. The buggy version should pass 4/8 checks; the historical fix should pass 8/8. Neither the grader nor the historical fix is uploaded.

On the isolated eval stack, run the same cases and model options against both backend sources:

```bash
# Older backend source: MODE=script-only. Newer backend source: MODE=mixed.
for model in halogen-flash-next gufo-flash-next; do
  ./scripts/eval.sh scripted-tools --model "$model" --reasoning-effort model-default \
    --modes direct "$MODE" --session-files --skip-safety --focus-read \
    --cases single pages repo-held-out --chain-length 12 --repeats 3
done
```

`observedMode` is inferred from the available schemas; `chosenMode` reports how the model actually called tools. A mixed offer does not require the model to use both paths. `toolSchemaTokens` sums the backend estimates for the recorded rounds, not provider-billed tokens. A mode mismatch makes the run invalid even if the task was completed correctly.

Keep the runs serial: the evaluator temporarily updates the existing model setting and restores it afterward. The comparison above does not include the separate replay and interruption probes, which check runtime-specific error and progress contracts.
