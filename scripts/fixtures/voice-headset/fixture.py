"""Behavior checks for the historical voice-headset visibility regression."""

import asyncio
import json
import subprocess
from pathlib import Path

DIRECTORY = Path(__file__).resolve().parent
FIXED = '30c5e335c3d64955ca21ec73644da825a8ce2c28'
ORIGINAL = '7d823ea7eb033c7438b962a1d14b7329a5073c21'
SOURCE = 'apps/frontend/src/features/chat/components/ChatComposer.vue'
FILES = ('voice-headset.js',)
PROMPT = (
    'Fix the headset visibility logic in voice-headset.js. Show the voice headset only after '
    'the provider list has finished loading and offers a usable realtime audio model. '
    'If a default realtime model ID is configured, it must be one of the available realtime '
    'audio options. With no default, any available realtime audio option is enough. '
    'Keep hiding the headset when the draft has content or voice is already active. '
    'The provider store exposes loading, lastLoadComplete, modelDefaults.realtimeModelId, '
    'and capabilityOptions(capability), which returns options whose value is the model ID. '
    'Keep the function name and arguments. Read the file, then use edit_file to fix it. '
    'Do not run the code; the evaluator tests the saved file. Briefly report the fix.'
)

# label, loading, lastLoadComplete, realtime option IDs, default ID, draft, voice, expected
TESTS = (
    ('no initial load', False, False, ['a'], None, False, False, False),
    ('refresh in progress', True, True, ['a'], None, False, False, False),
    ('no realtime models', False, True, [], None, False, False, False),
    ('missing configured model', False, True, ['a'], 'missing', False, False, False),
    ('matching configured model', False, True, ['a', 'b'], 'b', False, False, True),
    ('no configured default', False, True, ['a'], None, False, False, True),
    ('nonempty draft', False, True, ['a'], 'a', True, False, False),
    ('voice active', False, True, ['a'], 'a', False, True, False),
)


async def grade(files):
    """Run untrusted text only in the bounded JS worker, without tools."""
    from mato_loop_backend.agent.script_tools import ScriptArguments, run_script

    source = files.get(FILES[0])
    values = None
    if isinstance(source, str) and len(source.encode('utf-8')) <= 4096:
        body = '"use strict";\n' + source + '\nreturn showVoiceHeadset;'
        code = f'const show = new Function("computed", {json.dumps(body)})(fn => ({{get value() {{return fn();}}}}));\n'
        code += 'return ' + json.dumps([test[1:-1] for test in TESTS]) + '''.map(
  ([loading, lastLoadComplete, options, realtimeModelId, draft, voice]) => show({
    loading, lastLoadComplete, modelDefaults: {realtimeModelId},
    capabilityOptions: name => name === 'realtime_audio' ? options.map(value => ({value})) : [],
  }, {value: draft}, {value: voice}));'''

        async def reject_tool(*_):
            raise ValueError('The fixture does not expose tools.')

        try:
            result = await asyncio.wait_for(run_script(ScriptArguments(code=code), reject_tool), 5)
            payload = json.loads(result.content)
            if isinstance(payload, dict) and 'error' not in payload and payload.get('toolCalls') == 0:
                values = payload.get('result')
        except (TimeoutError, ValueError):
            pass
    return {
        test[0]: isinstance(values, list) and len(values) == len(TESTS) and values[index] is test[-1]
        for index, test in enumerate(TESTS)
    }


def historical_source(revision):
    source = subprocess.check_output(['git', 'show', f'{revision}:{SOURCE}'], cwd=DIRECTORY, text=True)
    start = source.index('const showVoiceStart = ')
    end = source.index('\nconst voiceSendLabel = ', start)
    computed = '\n'.join('  ' + line for line in source[start:end].splitlines())
    return ('function showVoiceHeadset(providers, draftHasContent, voiceActivity) {\n'
            f'{computed}\n  return showVoiceStart.value\n}}\n')
