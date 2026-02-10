from __future__ import annotations

from dataclasses import dataclass, field
import os
import time
from typing import Any, Awaitable, Callable, cast, overload

from chordcode.log import logger

from chordcode.hookdefs import (
    ChatHeadersInput,
    ChatHeadersOutput,
    ChatMessageInput,
    ChatMessageOutput,
    ChatParamsInput,
    ChatParamsOutput,
    ConfigInput,
    EventInput,
    ExperimentalChatMessagesTransformInput,
    ExperimentalChatMessagesTransformOutput,
    ExperimentalChatSystemTransformInput,
    ExperimentalChatSystemTransformOutput,
    Hook as HookName,
    PermissionAskInput,
    PermissionAskOutput,
    ToolExecuteAfterInput,
    ToolExecuteAfterOutput,
    ToolExecuteBeforeInput,
    ToolExecuteBeforeOutput,
)

Input = dict[str, Any]
Output = dict[str, Any]
HookFn = Callable[[Input, Output], Awaitable[None]]
Hooks = dict[str | HookName, HookFn]


@dataclass
class Hooker:
    items: list[Hooks] = field(default_factory=list)

    def add(self, hooks: Hooks) -> None:
        self.items.append(hooks)

    @overload
    async def trigger(self, name: HookName, input: ConfigInput, output: dict[str, Any]) -> dict[str, Any]: ...

    @overload
    async def trigger(self, name: HookName, input: EventInput, output: dict[str, Any]) -> dict[str, Any]: ...

    @overload
    async def trigger(self, name: HookName, input: ChatMessageInput, output: ChatMessageOutput) -> ChatMessageOutput: ...

    @overload
    async def trigger(self, name: HookName, input: ChatParamsInput, output: ChatParamsOutput) -> ChatParamsOutput: ...

    @overload
    async def trigger(self, name: HookName, input: ChatHeadersInput, output: ChatHeadersOutput) -> ChatHeadersOutput: ...

    @overload
    async def trigger(self, name: HookName, input: PermissionAskInput, output: PermissionAskOutput) -> PermissionAskOutput: ...

    @overload
    async def trigger(
        self,
        name: HookName,
        input: ToolExecuteBeforeInput,
        output: ToolExecuteBeforeOutput,
    ) -> ToolExecuteBeforeOutput: ...

    @overload
    async def trigger(self, name: HookName, input: ToolExecuteAfterInput, output: ToolExecuteAfterOutput) -> ToolExecuteAfterOutput: ...

    @overload
    async def trigger(
        self,
        name: HookName,
        input: ExperimentalChatSystemTransformInput,
        output: ExperimentalChatSystemTransformOutput,
    ) -> ExperimentalChatSystemTransformOutput: ...

    @overload
    async def trigger(
        self,
        name: HookName,
        input: ExperimentalChatMessagesTransformInput,
        output: ExperimentalChatMessagesTransformOutput,
    ) -> ExperimentalChatMessagesTransformOutput: ...

    async def trigger(self, name: str, input: Input, output: Output) -> Output:
        if not name or name == HookName.All:
            return output

        start = time.perf_counter()
        before = list(output.keys())

        for hooks in self.items:
            fn = hooks.get(HookName.All) or hooks.get("*")
            if fn:
                await fn({"hook": name, "phase": "before", "input": input}, output)

        for hooks in self.items:
            fn = hooks.get(name)
            if fn:
                await fn(input, output)

        ms = (time.perf_counter() - start) * 1000
        after = list(output.keys())

        for hooks in self.items:
            fn = hooks.get(HookName.All) or hooks.get("*")
            if fn:
                await fn(
                    {
                        "hook": name,
                        "phase": "after",
                        "ms": ms,
                        "out_keys_before": before,
                        "out_keys_after": after,
                        "input": input,
                    },
                    output,
                )
        return output


def loghook(*, enabled: bool | None = None) -> Hooks:
    on = enabled
    if on is None:
        on = os.environ.get("CHORDCODE_HOOK_DEBUG", "").strip() not in ("", "0", "false", "False")
    if not on:
        return {}

    async def emit(input: Input, output: Output) -> None:
        hook = input.get("hook")
        phase = input.get("phase")
        ms = input.get("ms")
        before = input.get("out_keys_before")
        after = input.get("out_keys_after")
        inp = input.get("input")

        msg = f"[hook] {hook} {phase}"
        if isinstance(ms, (int, float)):
            msg += f" {ms:.1f}ms"
        if before is not None or after is not None:
            msg += f" out={before}->{after}"
        if inp is not None:
            msg += f" in={list(inp.keys()) if isinstance(inp, dict) else type(inp).__name__}"

        logger.debug(msg, event="hook.debug")

    return {HookName.All: emit}
