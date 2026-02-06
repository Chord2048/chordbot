from __future__ import annotations

from typing import Optional
from chordcode.config import LangfuseConfig
from chordcode.log import log

_langfuse_instance: Optional["Langfuse"] = None


def init_langfuse(config: LangfuseConfig) -> Optional["Langfuse"]:
    """Initialize the global Langfuse client instance."""
    global _langfuse_instance
    
    if not config.enabled:
        _langfuse_instance = None
        return None
    
    if not config.public_key or not config.secret_key:
        log.bind(event="langfuse.disabled").warning(
            "LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set; tracing disabled",
        )
        _langfuse_instance = None
        return None
    
    try:
        from langfuse import Langfuse
        
        _langfuse_instance = Langfuse(
            public_key=config.public_key,
            secret_key=config.secret_key,
            host=config.base_url,
            environment=config.environment,
            sample_rate=config.sample_rate,
            debug=config.debug,
        )

        log.bind(event="langfuse.init", environment=config.environment, base_url=config.base_url).info(
            "Langfuse initialized",
        )
        return _langfuse_instance
    except ImportError:
        log.bind(event="langfuse.disabled").warning("langfuse package not installed; tracing disabled")
        _langfuse_instance = None
        return None
    except Exception as e:
        log.bind(event="langfuse.init.error").opt(exception=e).error("Error initializing Langfuse")
        _langfuse_instance = None
        return None


def get_langfuse() -> Optional["Langfuse"]:
    """Get the global Langfuse client instance."""
    return _langfuse_instance


def flush_langfuse() -> None:
    """Flush all pending events to Langfuse."""
    if _langfuse_instance is not None:
        try:
            _langfuse_instance.flush()
            log.bind(event="langfuse.flush").debug("Langfuse flushed")
        except Exception as e:
            log.bind(event="langfuse.flush.error").opt(exception=e).error("Error flushing Langfuse")


def shutdown_langfuse() -> None:
    """Shutdown the Langfuse client and flush pending events."""
    global _langfuse_instance
    
    if _langfuse_instance is not None:
        try:
            _langfuse_instance.flush()
            _langfuse_instance.shutdown()
            log.bind(event="langfuse.shutdown").debug("Langfuse shutdown")
        except Exception as e:
            log.bind(event="langfuse.shutdown.error").opt(exception=e).error("Error during Langfuse shutdown")
        finally:
            _langfuse_instance = None
