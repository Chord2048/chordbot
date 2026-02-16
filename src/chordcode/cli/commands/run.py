"""chordcode run — quick-run: create session, send message, run agent, stream output."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

import typer

from chordcode.cli.client import Client, APIError
from chordcode.cli.output import Output


async def _quick_run(
    *,
    base_url: str,
    message: str,
    model: str | None,
    permission: str,
    worktree: str,
    session_id: str | None,
    no_stream: bool,
    cleanup: bool,
    json_mode: bool,
) -> None:
    out = Output(json_mode=json_mode)
    client = Client(base_url)

    # 1. Check server reachability
    if not await client.ping():
        out.error("Cannot reach server.", hint=f"Start with `chordcode serve`. URL: {base_url}")
        raise typer.Exit(1)

    # 2. Resolve or create session
    temp_session = False
    if session_id:
        sid = session_id
    else:
        try:
            data = await client.post("/sessions", json={"worktree": worktree, "title": f"cli: {message[:40]}"})
            sid = data["id"]
            temp_session = True
        except APIError as e:
            out.error(f"Failed to create session: {e.detail}")
            raise typer.Exit(1)

    if not json_mode:
        out.info(f"Session: {sid}")

    # 3. Add user message
    try:
        await client.post(f"/sessions/{sid}/messages", json={"text": message})
    except APIError as e:
        out.error(f"Failed to send message: {e.detail}")
        raise typer.Exit(1)

    # 4. Start SSE listener + trigger run
    if no_stream:
        # Non-streaming: just run and get result
        try:
            data = await client.post(f"/sessions/{sid}/run")
            if json_mode:
                out.data(data)
            else:
                out.success(f"Run complete. Message: {data.get('assistant_message_id', '')[:12]}")
        except APIError as e:
            out.error(f"Run failed: {e.detail}")
    else:
        # Streaming: listen to SSE while run executes
        collected_text: list[str] = []
        run_done = asyncio.Event()
        run_result: dict = {}

        async def listen_events():
            try:
                async for evt in client.stream_sse("/events", params={"session_id": sid}):
                    etype = evt.get("type", "")
                    props = evt.get("properties", {})

                    if etype == "message.part.updated":
                        part = props.get("part", {})
                        delta = props.get("delta", "")
                        if part.get("type") == "text" and delta:
                            if json_mode:
                                print(json.dumps({"event": "text_delta", "text": delta}), flush=True)
                            else:
                                print(delta, end="", flush=True)
                            collected_text.append(delta)

                    elif etype == "message.part.updated" and props.get("part", {}).get("type") == "tool":
                        tool_part = props.get("part", {})
                        state = tool_part.get("state", {})
                        status = state.get("status", "")
                        tool_name = tool_part.get("tool", "")
                        if not json_mode:
                            if status == "running":
                                title = state.get("title", tool_name)
                                print(f"\n[tool: {title}]", flush=True)
                            elif status == "completed":
                                title = state.get("title", tool_name)
                                print(f"\n[tool done: {title}]", flush=True)
                            elif status == "error":
                                err = state.get("error", "unknown")
                                print(f"\n[tool error: {tool_name}: {err}]", flush=True)
                        else:
                            print(json.dumps({"event": "tool", "tool": tool_name, "status": status}), flush=True)

                    elif etype == "permission.requested":
                        if permission in ("allow", "deny"):
                            # Auto-reply to permission requests
                            req_id = props.get("id", "")
                            reply_action = "once" if permission == "allow" else "reject"
                            if req_id:
                                try:
                                    await client.post(f"/permissions/{req_id}/reply", json={"reply": reply_action})
                                    if not json_mode:
                                        print(f"\n[permission auto-{reply_action}: {props.get('permission', '')}]", flush=True)
                                except Exception:
                                    pass
                        else:
                            if not json_mode:
                                print(f"\n[permission pending: {props.get('permission', '')} — reply via `chordcode permissions reply`]", flush=True)

                    elif etype == "session.loop.done":
                        run_done.set()
                        return
                    elif etype == "session.interrupted":
                        if not json_mode:
                            print("\n[interrupted]", flush=True)
                        run_done.set()
                        return
            except Exception:
                run_done.set()

        async def trigger_run():
            try:
                result = await client.post(f"/sessions/{sid}/run")
                run_result.update(result)
            except APIError as e:
                out.error(f"Run failed: {e.detail}")
            finally:
                # Give listener time to process final events
                await asyncio.sleep(0.5)
                run_done.set()

        listener = asyncio.create_task(listen_events())
        await asyncio.sleep(0.2)  # let SSE connection establish
        runner = asyncio.create_task(trigger_run())

        try:
            await asyncio.wait_for(run_done.wait(), timeout=600)
        except asyncio.TimeoutError:
            out.warning("Run timed out after 600s.")

        listener.cancel()
        try:
            await listener
        except asyncio.CancelledError:
            pass

        if not json_mode:
            print()  # newline after streaming

        if json_mode and run_result:
            out.data(run_result)

    # 5. Cleanup temp session
    if temp_session and cleanup:
        try:
            await client.delete(f"/sessions/{sid}")
            if not json_mode:
                out.info("Temp session cleaned up.")
        except Exception:
            pass


def run_cmd(
    message: str = typer.Argument(..., help="User message to send."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model (not yet implemented)."),
    permission: str = typer.Option("ask", "--permission", "-p", help="Permission handling: allow | deny | ask."),
    worktree: str = typer.Option("", "--worktree", "-w", help="Worktree path (defaults to cwd)."),
    session_id: Optional[str] = typer.Option(None, "--session-id", "-s", help="Use existing session."),
    no_stream: bool = typer.Option(False, "--no-stream", help="Disable streaming; wait for completion."),
    cleanup: bool = typer.Option(True, "--cleanup/--no-cleanup", help="Delete temp session after run."),
):
    """Quick-run: create session, send message, run agent, stream output."""
    from chordcode.cli.app import state

    wt = worktree or os.getcwd()
    if not os.path.isabs(wt):
        wt = os.path.abspath(wt)

    asyncio.run(
        _quick_run(
            base_url=state.base_url,
            message=message,
            model=model,
            permission=permission,
            worktree=wt,
            session_id=session_id,
            no_stream=no_stream,
            cleanup=cleanup,
            json_mode=state.json_mode,
        )
    )
