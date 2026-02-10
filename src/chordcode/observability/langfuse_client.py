from __future__ import annotations

from typing import Optional
from chordcode.config import LangfuseConfig
from chordcode.log import logger

_langfuse_instance: Optional["Langfuse"] = None


def init_langfuse(config: LangfuseConfig) -> Optional["Langfuse"]:
    """Initialize the global Langfuse client instance."""
    global _langfuse_instance
    
    if not config.enabled:
        _langfuse_instance = None
        return None
    
    if not config.public_key or not config.secret_key:
        logger.warning(
            "LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY not set; tracing disabled",
            event="langfuse.disabled",
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

        logger.info(
            "Langfuse initialized",
            event="langfuse.init",
            environment=config.environment,
            base_url=config.base_url,
        )
        return _langfuse_instance
    except ImportError:
        logger.warning("langfuse package not installed; tracing disabled", event="langfuse.disabled")
        _langfuse_instance = None
        return None
    except Exception as e:
        logger.error("Error initializing Langfuse", event="langfuse.init.error", exc_info=e)
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
            logger.debug("Langfuse flushed", event="langfuse.flush")
        except Exception as e:
            logger.error("Error flushing Langfuse", event="langfuse.flush.error", exc_info=e)


def shutdown_langfuse() -> None:
    """Shutdown the Langfuse client and flush pending events."""
    global _langfuse_instance
    
    if _langfuse_instance is not None:
        try:
            _langfuse_instance.flush()
            _langfuse_instance.shutdown()
            logger.debug("Langfuse shutdown", event="langfuse.shutdown")
        except Exception as e:
            logger.error("Error during Langfuse shutdown", event="langfuse.shutdown.error", exc_info=e)
        finally:
            _langfuse_instance = None
