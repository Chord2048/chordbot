from __future__ import annotations

from typing import Any
from chordcode.hookdefs import Hook
from chordcode.hooks import Hooks
from chordcode.observability.langfuse_client import get_langfuse
from chordcode.log import logger


def create_langfuse_hook() -> Hooks:
    """
    Create a Langfuse hook that logs various events to Langfuse.
    This hook captures ChatParams, ChatHeaders, ToolExecuteBefore/After, and other events.
    
    Note: In SDK v3, we don't create standalone events. Instead, we update the current observation's metadata.
    """
    langfuse = get_langfuse()
    
    # If Langfuse is not enabled, return empty hooks
    if not langfuse:
        return {}
    
    async def on_chat_params(input: dict[str, Any], output: dict[str, Any]) -> None:
        """Update current observation with chat parameters."""
        try:
            # In SDK v3, update the current observation's metadata
            langfuse.update_current_span(
                metadata={
                    "chat_params": {
                        "temperature": output.get("temperature"),
                        "top_p": output.get("top_p"),
                        "top_k": output.get("top_k"),
                    }
                }
            )
        except Exception as e:
            logger.error("Error logging chat params", event="langfuse.hook.chat_params.error", exc_info=e)
    
    async def on_chat_headers(input: dict[str, Any], output: dict[str, Any]) -> None:
        """Update current observation with custom chat headers."""
        try:
            headers = output.get("headers", {})
            if headers:
                langfuse.update_current_span(
                    metadata={
                        "chat_headers": dict(headers) if isinstance(headers, dict) else {}
                    }
                )
        except Exception as e:
            logger.error("Error logging chat headers", event="langfuse.hook.chat_headers.error", exc_info=e)
    
    async def on_tool_execute_before(input: dict[str, Any], output: dict[str, Any]) -> None:
        """Update current observation with tool execution start info."""
        try:
            tool_name = input.get("tool")
            call_id = input.get("call_id")
            args = output.get("args", {})
            
            if tool_name:
                langfuse.update_current_span(
                    metadata={
                        "tool_execute_before": {
                            "tool": tool_name,
                            "call_id": call_id,
                            "args_keys": list(args.keys()) if isinstance(args, dict) else [],
                        }
                    }
                )
        except Exception as e:
            logger.error("Error logging tool execute before", event="langfuse.hook.tool_before.error", exc_info=e)
    
    async def on_tool_execute_after(input: dict[str, Any], output: dict[str, Any]) -> None:
        """Update current observation with tool execution completion info."""
        try:
            tool_name = input.get("tool")
            call_id = input.get("call_id")
            title = output.get("title")
            metadata = output.get("metadata", {})
            
            if tool_name:
                langfuse.update_current_span(
                    metadata={
                        "tool_execute_after": {
                            "tool": tool_name,
                            "call_id": call_id,
                            "title": title,
                            "result_metadata": metadata,
                        }
                    }
                )
        except Exception as e:
            logger.error("Error logging tool execute after", event="langfuse.hook.tool_after.error", exc_info=e)
    
    async def on_system_transform(input: dict[str, Any], output: dict[str, Any]) -> None:
        """Update current observation with system prompt transformation info."""
        try:
            system_prompts = output.get("system", [])
            
            if system_prompts:
                langfuse.update_current_span(
                    metadata={
                        "system_prompt_transform": {
                            "prompt_count": len(system_prompts) if isinstance(system_prompts, list) else 1,
                        }
                    }
                )
        except Exception as e:
            logger.error("Error logging system transform", event="langfuse.hook.system_transform.error", exc_info=e)
    
    async def on_messages_transform(input: dict[str, Any], output: dict[str, Any]) -> None:
        """Update current observation with message transformation info."""
        try:
            messages = output.get("messages", [])
            
            if messages:
                langfuse.update_current_span(
                    metadata={
                        "messages_transform": {
                            "message_count": len(messages) if isinstance(messages, list) else 0,
                        }
                    }
                )
        except Exception as e:
            logger.error("Error logging messages transform", event="langfuse.hook.messages_transform.error", exc_info=e)
    
    # Return the hooks dictionary
    return {
        Hook.ChatParams: on_chat_params,
        Hook.ChatHeaders: on_chat_headers,
        Hook.ToolExecuteBefore: on_tool_execute_before,
        Hook.ToolExecuteAfter: on_tool_execute_after,
        Hook.ExperimentalChatSystemTransform: on_system_transform,
        Hook.ExperimentalChatMessagesTransform: on_messages_transform,
    }
