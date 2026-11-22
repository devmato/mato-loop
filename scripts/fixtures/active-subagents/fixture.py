"""Offline proof and isolated-worker inputs for a historical chat regression."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
ORIGINAL = "85c8509a5e098cf3e8bbeec85a2be97104969509"
FIXED = "2be46c78aa5142b2220584db51c56ab26d258552"
FILES = ("chat-page.js", "activity-panel.js")
PROMPT = (
    "Fix the simplified chat activity code in chat-page.js and activity-panel.js. "
    "After an intervening user message and a newer assistant reply, active subagents should appear "
    "only on the latest assistant reply, even if that reply has no work history or a user message follows it. "
    "In collapsed history, hide an older spawn row if it only exists to display live subagents; "
    "do not show an older fallback live row either. In expanded history, still show the original spawn "
    "entry with its matching children (match both run and tool call), including finished children. "
    "Keep ordinary entries. Do not show finished subagents as live or duplicate live rows already shown in current entries. "
    "Preserve the panel's default when called on its own and keep existing function signatures. "
    "Read both files before editing. Use short exact replacements with edit_file instead of rewriting whole functions. "
    "Do not run the code; the evaluator checks the saved files. "
    "Briefly report the fix."
)

# This harness runs in the worker. It reduces Vue rendering to visible IDs and supplies
# already-prepared panel entries; it does not implement the behaviors being graded.
HARNESS = r"""
const computed = (fn) => ({ get value() { return fn(); } });
const active = new Set(['queued', 'running', 'waiting_approval', 'waiting_input']);
const isActiveSubagent = (child) => active.has(child.run?.status);
const {chatPanels, panelRows} = new Function('computed', 'isActiveSubagent', CANDIDATE)(computed, isActiveSubagent);
const child = (id, status, run = 'run-1', tool = 'spawn') => ({
  id, run: {status}, spawnedByRunId: run, spawnedByToolCallId: tool
});
const children = [child('running', 'running'), child('finished', 'completed'),
  child('waiting', 'waiting_input', 'run-2'), child('queued', 'queued', 'run-3', 'different'),
  child('approval', 'waiting_approval', 'run-1', 'different')];
const spawn = {key: 'start', type: 'tool', runId: 'run-1', tool: {id: 'spawn'}};
const ordinary = {key: 'read', type: 'tool', runId: 'run-1', tool: {id: 'read'}};
const messages = [
  {id: 'old', role: 'assistant', workHistory: {entries: [spawn, ordinary]}},
  {id: 'direct', role: 'user'}, {id: 'latest', role: 'assistant', content: 'Acknowledged.'},
  {id: 'trailing', role: 'user'}
];
const [older, , latest] = chatPanels(messages, children);
const assigned = older.subagents.filter((item) => item.spawnedByRunId === spawn.runId
  && item.spawnedByToolCallId === spawn.tool.id);
const historical = [{...spawn, subagents: assigned}, ordinary];
const ids = (items) => items.map((item) => item.id);
const visible = ({rows, entries}) => [ids(rows), entries.map((entry) => [entry.key, ids(entry.subagents || [])])];
return [
  visible(panelRows(latest.history, false, [], [], latest.subagents, latest.showActiveSubagents)),
  visible(panelRows(older.history, false, historical, historical, older.subagents, older.showActiveSubagents)),
  visible(panelRows(older.history, false, historical, [], older.subagents, older.showActiveSubagents)),
  [ids(older.subagents), ...visible(panelRows(older.history, true, historical, [], older.subagents, older.showActiveSubagents))],
  visible(panelRows(null, false, [], [], children)),
  visible(panelRows(null, false, [], [], children, false)),
  visible(panelRows(older.history, false, historical, historical, assigned))
];
"""
ACTIVE = ["running", "waiting", "queued", "approval"]
EXPECTED = {
    "latest assistant without history (followed by user message)": [ACTIVE, []],
    "older collapsed history hides the live spawn row but keeps ordinary entries": [[], [["read", []]]],
    "older collapsed fallback has no live child": [[], []],
    "expanded spawn history preserves exact run/tool matches": [
        ["running", "finished"], [], [["start", ["running", "finished"]], ["read", []]]
    ],
    "standalone panel defaults to live": [ACTIVE, []],
    "standalone panel can disable live rows": [[], []],
    "current entry shows running child without fallback duplicate": [[], [["start", ["running"]], ["read", []]]],
}


def worker_code(files):
    """Pass this body to the existing isolated JS worker with tool dispatch rejected."""
    sources = '"use strict";\n' + "\n".join(files[name] for name in FILES)
    return "const CANDIDATE = " + json.dumps(sources + "\nreturn {chatPanels, panelRows};") + ";\n" + HARNESS


def checks(values):
    return {
        name: isinstance(values, list) and len(values) == len(EXPECTED) and values[index] == expected
        for index, (name, expected) in enumerate(EXPECTED.items())
    }


async def observe(files):
    """Evaluate persisted UTF-8 artifacts in the worker without tool dispatch."""
    from mato_loop_backend.agent.script_tools import ScriptArguments, run_script

    if any(not isinstance(files.get(name), str) or len(files[name].encode()) > 4096 for name in FILES):
        return None

    async def reject_tool(*_):
        raise ValueError("The fixture does not expose tools.")

    try:
        result = await asyncio.wait_for(run_script(ScriptArguments(code=worker_code(files)), reject_tool), 5)
        payload = json.loads(result.content)
        if isinstance(payload, dict) and "error" not in payload and payload.get("toolCalls") == 0:
            return payload.get("result")
    except (TimeoutError, ValueError):
        pass
    return None


async def grade(files):
    return checks(await observe(files))


def correction_prompt(values):
    # Never feed arbitrary model-authored strings back as test diagnostics.
    def observed(actual):
        text = json.dumps(bounded(actual))
        return text if len(text) <= 400 else 'unexpected result shape (omitted)'

    def bounded(value, depth=0):
        if isinstance(value, list) and depth < 6 and len(value) <= 12:
            return [bounded(item, depth + 1) for item in value]
        if isinstance(value, str) and value in {"start", "read", "finished", *ACTIVE}:
            return value
        return "<unexpected value>"

    if not isinstance(values, list) or len(values) != len(EXPECTED):
        return (
            "The saved files did not produce a valid test result. Expected seven behavior results; "
            "actual: no valid results. Check syntax and the existing function signatures. "
            "Make one correction attempt on the current files and report what you changed."
        )
    failures = [
        f"- {name}: expected {json.dumps(expected)}; actual {observed(actual)}"
        for (name, expected), actual in zip(EXPECTED.items(), values, strict=True)
        if expected != actual
    ]
    return (
        "The evaluator tested your saved patch. Only the failed checks are listed below. "
        "Rows and child lists contain IDs; each history entry is [entry ID, child IDs]. "
        "Make one correction attempt on the current session files (root=files) with edit_file. "
        "Do not rerun or repeat an already applied edit. Read the current files before changing them. "
        "Do not use the web or delegate. Briefly report the correction.\n" + "\n".join(failures)
    )


def _slice(text, start, stop):
    return text[text.index(start):text.index(stop, text.index(start))].rstrip()


def _indent(text):
    return "\n".join("  " + line for line in text.splitlines()) + "\n"


def _historical_sources(rev):
    def source(path):
        return subprocess.check_output(
            ["git", "show", f"{rev}:apps/frontend/src/features/{path}"], cwd=DIRECTORY, text=True
        )

    chat = source("chat/ChatPage.vue")
    panel = source("chat/components/AgentActivityPanel.vue")
    store = source("subagents/store.js")
    assert "const ACTIVE = new Set(['queued', 'running', 'waiting_approval', 'waiting_input'])" in store
    assert "export const isActiveSubagent = (child) => ACTIVE.has(child.run?.status)" in store
    page_head = "const latestAssistantMessageId" if "const latestAssistantMessageId" in chat else "const childrenByMessage"
    mapping = _slice(chat, page_head, "const unmatchedChildren")
    binding = ':show-active-subagents="message.id === latestAssistantMessageId"' in chat
    assert ':subagents="childrenByMessage.get(message.id) || emptyItems"' in chat
    no_history = _slice(panel, "<template>", "  <div\n")
    assert '<SubagentRows v-if="!historyExpanded" :children="activeSubagents"' in panel
    assert 'v-for="entry in displayEntries"' in panel
    assert '<slot v-if="!history" />' in no_history or '<SubagentRows :children="activeSubagents"' in no_history
    if binding:
        assert "showActiveSubagents: { type: Boolean, default: true }" in panel
    return {
        FILES[0]: (
            "function chatPanels(messages, children) {\n"
            "  const transcript = { value: { messages } }\n  const subagents = { children }\n"
            + _indent(mapping) + "  return messages.map((message) => ({\n    id: message.id,\n"
            "    history: message.workHistory,\n    subagents: childrenByMessage.value.get(message.id) || []"
            + (",\n    showActiveSubagents: message.id === latestAssistantMessageId.value" if binding else "")
            + "\n  }))\n}\n"
        ),
        FILES[1]: (
            "function panelRows(history, expanded, all, current, subagents, showActiveSubagents = true) {\n"
            "  const props = { subagents, showActiveSubagents }\n  const historyExpanded = { value: expanded }\n"
            "  const entries = { value: all }\n  const currentEntries = { value: current }\n"
            "  const currentBatch = { value: null }\n"
            + _indent(_slice(panel, "const displayEntries", "const items"))
            + "  if (!history) return { rows: "
            + ("activeSubagents.value" if '<SubagentRows :children="activeSubagents"' in no_history else "[]")
            + ", entries: [] }\n  return {\n    rows: historyExpanded.value ? [] : activeSubagents.value,\n"
            "    entries: displayEntries.value\n  }\n}\n"
        ),
    }


def main(node):
    original, fixed = map(_historical_sources, (ORIGINAL, FIXED))
    for name in FILES:
        assert (DIRECTORY / name).read_bytes() == original[name].encode(), name
    # Standalone Node is only for this proof: every executed source below is built from Git.
    for label, files, expected in (
        ("original", original, (0, 0, 0, 1, 0, 1, 1)),
        ("page-only", {**original, FILES[0]: fixed[FILES[0]]}, (0, 0, 0, 1, 0, 1, 1)),
        ("panel-only", {**original, FILES[1]: fixed[FILES[1]]}, (0, 0, 0, 1, 1, 1, 1)),
        ("fixed", fixed, (1, 1, 1, 1, 1, 1, 1)),
    ):
        output = subprocess.check_output(
            [node, "-e", "console.log(JSON.stringify((() => {" + worker_code(files) + "})()));"], timeout=5
        )
        results = checks(json.loads(output))
        print(f"{label}: {sum(results.values())}/{len(results)} passed")
        assert tuple(results.values()) == expected, results


if __name__ == "__main__":
    if sys.argv[1:] == ["--fixed-json"]:
        original = _historical_sources(ORIGINAL)
        for name in FILES:
            assert (DIRECTORY / name).read_bytes() == original[name].encode(), name
        print(json.dumps(_historical_sources(FIXED)))
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "node")
