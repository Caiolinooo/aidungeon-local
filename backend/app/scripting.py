"""Sandboxed scripting engine for AI Dungeon Local (Milestone 4 / R4).

Provides three hooks:
- run_input_modifier(script, action, timeout=1.0) -> dict
- run_context_modifier(script, context, timeout=1.0) -> list[dict]
- run_output_modifier(script, text, timeout=1.0) -> str

Sandbox Invariants:
1. AST Static Inspection: Rejects imports, dunder attribute traversal, and dangerous builtins.
2. Sterile Execution: Minimal builtins namespace, no filesystem/process/network access.
3. Timeout: Strict 1.0s timeout with process kill to abort infinite loops.
4. Fail-Safe: Any error, syntax failure, security rejection, or timeout logs a warning
   and returns the original unmodified input.
"""

from __future__ import annotations

import ast
import copy
import logging
import math
import multiprocessing as mp
from typing import Any

from app import db

log = logging.getLogger("aidungeon.scripting")

DANGEROUS_NAMES = {
    "eval", "exec", "open", "compile", "globals", "locals",
    "getattr", "setattr", "delattr", "__import__", "breakpoint",
    "memoryview", "input", "exit", "quit", "help",
    "classmethod", "staticmethod", "property", "super",
}

DANGEROUS_ATTRIBUTES = {
    "__builtins__", "__class__", "__subclasses__", "__code__",
    "__globals__", "__closure__", "__bases__", "__mro__",
    "__dict__", "__init_subclass__", "__reduce__", "__reduce_ex__",
}

SAFE_BUILTINS: dict[str, Any] = {
    # Types & constructors
    "abs": abs,
    "all": all,
    "any": any,
    "bin": bin,
    "bool": bool,
    "chr": chr,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "format": format,
    "frozenset": frozenset,
    "hex": hex,
    "int": int,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "oct": oct,
    "ord": ord,
    "pow": pow,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    # Standard exceptions
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "ZeroDivisionError": ZeroDivisionError,
    # Constants
    "True": True,
    "False": False,
    "None": None,
    # Harmless helpers
    "print": lambda *args, **kwargs: None,
}


class SecurityViolation(Exception):
    """Raised when script violates sandbox security policies."""


class SandboxASTValidator(ast.NodeVisitor):
    """AST validator to reject imports, dangerous builtins, and dunder access."""

    def visit_Import(self, node: ast.Import) -> None:
        raise SecurityViolation("import statement is disallowed in sandboxed scripts")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        raise SecurityViolation("import from statement is disallowed in sandboxed scripts")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.startswith("__") or node.attr in DANGEROUS_ATTRIBUTES:
            raise SecurityViolation(f"access to attribute '{node.attr}' is disallowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__") or node.id in DANGEROUS_NAMES:
            raise SecurityViolation(f"access to identifier '{node.id}' is disallowed")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and (node.value.startswith("__") or node.value in DANGEROUS_ATTRIBUTES):
            raise SecurityViolation(f"string constant '{node.value}' is disallowed")
        self.generic_visit(node)


def validate_script_ast(script: str) -> None:
    """Validate that script contains no forbidden constructs."""
    tree = ast.parse(script)
    SandboxASTValidator().visit(tree)


def _sandbox_worker(pipe: Any, hook_type: str, script_code: str, payload: Any) -> None:
    """Worker process entry point executing the script in a sterile namespace."""
    try:
        ns: dict[str, Any] = {
            "__builtins__": SAFE_BUILTINS,
            "math": math,
        }

        if hook_type == "input":
            action = copy.deepcopy(payload) if isinstance(payload, dict) else {}
            ns.update({
                "action": action,
                "text": action.get("text", ""),
                "type": action.get("type", ""),
            })
        elif hook_type == "context":
            context = copy.deepcopy(payload) if isinstance(payload, list) else []
            ns["context"] = context
        elif hook_type == "output":
            text_str = str(payload) if payload is not None else ""
            ns.update({
                "text": text_str,
                "output": text_str,
            })

        compiled = compile(script_code, "<sandboxed_script>", "exec")
        exec(compiled, ns)

        if hook_type == "input":
            fn = ns.get("modifier") or ns.get("modify") or ns.get("input_modifier")
            if callable(fn):
                res = fn(copy.deepcopy(ns["action"]))
                if isinstance(res, dict):
                    pipe.send(("ok", res))
                    return
            action_res = ns.get("action")
            if isinstance(action_res, dict):
                # If text or type variable in global scope changed, sync it back
                if isinstance(payload, dict):
                    if ns.get("text") != payload.get("text") and action_res.get("text") == payload.get("text"):
                        action_res["text"] = ns["text"]
                    if ns.get("type") != payload.get("type") and action_res.get("type") == payload.get("type"):
                        action_res["type"] = ns["type"]
                pipe.send(("ok", action_res))
                return
            pipe.send(("ok", payload))

        elif hook_type == "context":
            fn = ns.get("modifier") or ns.get("modify") or ns.get("context_modifier")
            if callable(fn):
                res = fn(copy.deepcopy(ns["context"]))
                if isinstance(res, list):
                    pipe.send(("ok", res))
                    return
            ctx_res = ns.get("context")
            if isinstance(ctx_res, list):
                pipe.send(("ok", ctx_res))
                return
            pipe.send(("ok", payload))

        elif hook_type == "output":
            fn = ns.get("modifier") or ns.get("modify") or ns.get("output_modifier")
            if callable(fn):
                res = fn(str(payload))
                if isinstance(res, str):
                    pipe.send(("ok", res))
                    return
            out = ns.get("text")
            if isinstance(out, str) and out != payload:
                pipe.send(("ok", out))
                return
            out2 = ns.get("output")
            if isinstance(out2, str) and out2 != payload:
                pipe.send(("ok", out2))
                return
            if isinstance(out, str):
                pipe.send(("ok", out))
                return
            pipe.send(("ok", payload))

    except Exception as exc:
        pipe.send(("err", f"{type(exc).__name__}: {exc}"))


def _execute_sandboxed(hook_type: str, script: str, payload: Any, timeout: float = 1.0) -> Any:
    """Executes script in an isolated subprocess with AST check and timeout.

    Always fail-safe: returns original payload on any failure or timeout.
    """
    if not script or not isinstance(script, str) or not script.strip():
        return payload

    try:
        validate_script_ast(script)
    except Exception as exc:
        log.warning("Scripting %s modifier rejected by AST validator: %s", hook_type, exc)
        return payload

    effective_timeout = max(0.01, float(timeout if timeout is not None else 1.0))
    mp_ctx = mp.get_context("spawn")
    parent_conn, child_conn = mp_ctx.Pipe(duplex=False)
    proc = mp_ctx.Process(target=_sandbox_worker, args=(child_conn, hook_type, script, payload))

    try:
        proc.start()
        child_conn.close()
        proc.join(timeout=effective_timeout)

        if proc.is_alive():
            log.warning("Scripting %s modifier timed out after %.2fs", hook_type, effective_timeout)
            proc.terminate()
            proc.join(timeout=0.05)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=0.05)
            parent_conn.close()
            return payload

        if parent_conn.poll():
            status, res = parent_conn.recv()
            if status == "ok":
                return res
            log.warning("Scripting %s modifier execution error: %s", hook_type, res)
            return payload

    except Exception as exc:
        log.warning("Scripting %s modifier subprocess error: %s", hook_type, exc)
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
        if proc.is_alive():
            try:
                proc.kill()
            except Exception:
                pass

    return payload


def run_input_modifier(script: str, action: dict, timeout: float = 1.0) -> dict:
    """Run sandboxed input modifier hook. Fail-safe returns unmodified action."""
    result = _execute_sandboxed("input", script, action, timeout=timeout)
    return result if isinstance(result, dict) else action


def run_context_modifier(script: str, context: list[dict], timeout: float = 1.0) -> list[dict]:
    """Run sandboxed context modifier hook. Fail-safe returns unmodified context."""
    result = _execute_sandboxed("context", script, context, timeout=timeout)
    return result if isinstance(result, list) else context


def run_output_modifier(script: str, text: str, timeout: float = 1.0) -> str:
    """Run sandboxed output modifier hook. Fail-safe returns unmodified text."""
    result = _execute_sandboxed("output", script, text, timeout=timeout)
    return result if isinstance(result, str) else text


# --- Adventure Script Storage Helpers ---

def get_adventure_script(adventure_id: str, hook: str) -> str | None:
    """Retrieve an adventure-specific script hook from the settings table."""
    try:
        with db.session() as conn:
            return db.get_setting(conn, f"script:{adventure_id}:{hook}")
    except Exception as exc:
        log.warning("Could not read script for adventure %s hook %s: %s", adventure_id, hook, exc)
        return None


def set_adventure_script(adventure_id: str, hook: str, script: str) -> None:
    """Store an adventure-specific script hook in the settings table."""
    with db.session() as conn:
        if script and script.strip():
            db.set_setting(conn, f"script:{adventure_id}:{hook}", script)
        else:
            db.delete_setting(conn, f"script:{adventure_id}:{hook}")
