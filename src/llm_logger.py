"""
llm_logger.py — LangChain callback that logs every LLM request and response
to a dedicated file, verbatim, without altering any behaviour.

Usage:
    from llm_logger import LLMFileLogger
    llm = ChatOpenAI(..., callbacks=[LLMFileLogger()])

Log file is configured via LLM_LOG_FILE env var.
Default: /var/lib/system-monitor/llm.log
"""

import logging
import os
from datetime import datetime
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

LLM_LOG_FILE = os.environ.get(
    "LLM_LOG_FILE",
    "/var/lib/system-monitor/llm.log",
)

# Dedicated file logger — separate from the main telemon stdout logger
_file_log = logging.getLogger("llm_file")
_file_log.setLevel(logging.DEBUG)
_file_log.propagate = False   # don't bubble up to the root logger

if not _file_log.handlers:
    import os as _os
    _os.makedirs(_os.path.dirname(LLM_LOG_FILE), exist_ok=True)
    _handler = logging.FileHandler(LLM_LOG_FILE, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _file_log.addHandler(_handler)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sep(char: str = "─", width: int = 60) -> str:
    return char * width


class LLMFileLogger(BaseCallbackHandler):
    """Logs every LLM call (prompt + response) to LLM_LOG_FILE verbatim."""

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        model = (serialized.get("kwargs") or {}).get("model") or \
                (serialized.get("kwargs") or {}).get("model_name") or "unknown"
        lines = [
            _sep("═"),
            f"[{_now()}]  LLM REQUEST  model={model}  run={str(run_id)[:8]}",
            _sep(),
        ]
        for i, turn in enumerate(messages):
            for msg in turn:
                role = getattr(msg, "type", "?")
                lines.append(f"[{role.upper()}]")
                lines.append(str(msg.content))
        lines.append(_sep())
        _file_log.debug("\n".join(lines))

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        lines = [
            f"[{_now()}]  LLM RESPONSE  run={str(run_id)[:8]}",
            _sep(),
        ]
        for gen_list in response.generations:
            for gen in gen_list:
                text = getattr(gen, "text", None) or \
                       str(getattr(gen, "message", gen))
                lines.append(text)
        lines.append(_sep("═"))
        _file_log.debug("\n".join(lines))

    def on_llm_error(
        self,
        error: Exception,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        _file_log.debug(
            "\n".join([
                _sep("═"),
                f"[{_now()}]  LLM ERROR  run={str(run_id)[:8]}",
                _sep(),
                str(error),
                _sep("═"),
            ])
        )
