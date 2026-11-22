"""QuickJS worker. The guest has no WASI mounts, module loader or ambient tool access."""

import asyncio
import json
import sys

from quickjs_rs import QuickJSError, Runtime, SourceTransform


def receive():
    return json.loads(sys.stdin.readline())


def send(kind, value):
    print(json.dumps([kind, value], ensure_ascii=True, allow_nan=False), flush=True)


async def main():
    source = receive()
    with Runtime(memory_limit=16 * 1024 * 1024, transform_flags=SourceTransform.NONE) as runtime:
        with runtime.new_context() as context:
            @context.function(name="__matoloop_call")
            def call(name, encoded):
                if (not isinstance(name, str) or len(name) > 200
                        or not isinstance(encoded, str) or len(encoded) > 96_000):
                    raise ValueError("Invalid tool call")
                send("call", [name, encoded])
                return receive()

            # Compile the function body separately so script text cannot escape the wrapper.
            context.globals["__matoloop_source"] = source
            try:
                value = await context.eval_async(r"""
                    const invoke = globalThis.__matoloop_call;
                    const source = globalThis.__matoloop_source;
                    delete globalThis.__matoloop_call;
                    delete globalThis.__matoloop_source;
                    const encode = JSON.stringify;
                    const callTool = async (name, args = {}) => invoke(name, encode(args));
                    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
                    encode((await new AsyncFunction('callTool', '"use strict";\n' + source)(callTool)) ?? null);
                """, timeout=float("inf"))  # The host bounds worker time, not dispatch waits.
            except QuickJSError:
                send("error", "JavaScript")
                return
            if not isinstance(value, str) or len(value) > 16_000:
                send("error", "OutputLimit")
                return
            send("result", value)


if __name__ == "__main__":
    asyncio.run(main())
