from pathlib import Path
import traceback
from typing import Any, Dict, List, cast
from interpreter import OpenInterpreter


OUTPUT_PATH = Path("messages.json")


def command_to_interpreter(cmd: Dict[str, Any]) -> OpenInterpreter:
    from .profile import interpreter

    interpreter.llm.model = cmd.get("model", interpreter.llm.model)  # type: ignore
    interpreter.llm.context_window = cmd.get("context_window", interpreter.llm.context_window)  # type: ignore
    interpreter.llm.api_base = cmd.get("api_base", interpreter.llm.api_base)  # type: ignore
    interpreter.llm.api_key = cmd.get("api_key", interpreter.llm.api_key)  # type: ignore
    interpreter.auto_run = cmd.get("auto_run", interpreter.auto_run)  # type: ignore
    interpreter.os = cmd.get("os_mode", interpreter.os)  # type: ignore
    interpreter.custom_instructions = cmd.get("custom_instructions", interpreter.custom_instructions)  # type: ignore
    interpreter.llm.supports_functions = cmd.get("supports_functions", interpreter.llm.supports_functions)
    return interpreter


def run(command: Dict[str, Any]) -> None:
    interpreter = command_to_interpreter(command)

    try:
        interpreter.server()
    finally:
        interpreter.computer.terminate()
    