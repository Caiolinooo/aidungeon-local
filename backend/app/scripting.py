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
import json
import logging
import math
import multiprocessing as mp
from typing import Any

from app import db

log = logging.getLogger("aidungeon.scripting")

SCRIPT_HOOKS = ("input_modifier", "context_modifier", "output_modifier")
CHAIN_PREFIX = "# aidungeon-mod-chain-v1\n"

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


def chain_parts(script: str) -> list[str] | None:
    """Envelope de composição. Script comum devolve None e segue o caminho único."""
    if not isinstance(script, str) or not script.startswith(CHAIN_PREFIX):
        return None
    try:
        data = json.loads(script[len(CHAIN_PREFIX):])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list) or len(data) < 2 or not all(isinstance(item, str) for item in data):
        return None
    return data


def compose_hook_scripts(base: str | None, mod_scripts: list[str] | None) -> str | None:
    """Base primeiro, mods válidos depois.

    Sem mod aceito, devolve a base intacta (None, vazio ou o texto salvo).
    Um único trecho também volta cru, para o sandbox antigo executar igual.
    Dois ou mais viram um envelope que o executor roda em sequência.
    """
    accepted: list[str] = []
    for raw in mod_scripts or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            validate_script_ast(raw)
        except (SecurityViolation, SyntaxError, ValueError) as exc:
            log.warning("skipping mod script rejected by sandbox: %s", exc)
            continue
        accepted.append(raw)
    if not accepted:
        return base
    parts: list[str] = []
    if isinstance(base, str) and base.strip():
        parts.append(base)
    parts.extend(accepted)
    if len(parts) == 1:
        return parts[0]
    return CHAIN_PREFIX + json.dumps(parts, ensure_ascii=False)


def _first_callable(ns: dict, names: tuple[str, ...]):
    for name in names:
        fn = ns.get(name)
        if callable(fn):
            return fn
    return None


def _pop_hook_fns(ns: dict) -> None:
    for name in ("modifier", "modify", "input_modifier", "context_modifier", "output_modifier"):
        ns.pop(name, None)


def _finish_input_part(ns: dict, before_action: dict | None) -> None:
    fn = _first_callable(ns, ("modifier", "modify", "input_modifier"))
    replaced = False
    if fn is not None and isinstance(ns.get("action"), dict):
        res = fn(copy.deepcopy(ns["action"]))
        if isinstance(res, dict):
            ns["action"] = res
            replaced = True
    action_res = ns.get("action")
    if not replaced and isinstance(action_res, dict) and isinstance(before_action, dict):
        if ns.get("text") != before_action.get("text") and action_res.get("text") == before_action.get("text"):
            action_res["text"] = ns["text"]
        if ns.get("type") != before_action.get("type") and action_res.get("type") == before_action.get("type"):
            action_res["type"] = ns["type"]
    if isinstance(ns.get("action"), dict):
        ns["text"] = ns["action"].get("text", "")
        ns["type"] = ns["action"].get("type", "")


def _finish_context_part(ns: dict) -> None:
    fn = _first_callable(ns, ("modifier", "modify", "context_modifier"))
    if fn is not None and isinstance(ns.get("context"), list):
        res = fn(copy.deepcopy(ns["context"]))
        if isinstance(res, list):
            ns["context"] = res


def _finish_output_part(ns: dict, before_text: str) -> None:
    fn = _first_callable(ns, ("modifier", "modify", "output_modifier"))
    if fn is not None:
        res = fn(before_text if isinstance(before_text, str) else "")
        if isinstance(res, str):
            ns["text"] = res
            ns["output"] = res
            return
    text = ns.get("text")
    output = ns.get("output")
    if isinstance(text, str) and text != before_text:
        ns["output"] = text
        return
    if isinstance(output, str) and output != before_text:
        ns["text"] = output


def _sandbox_worker_chain(pipe: Any, hook_type: str, parts: list[str], payload: Any) -> None:
    """Roda cada trecho no mesmo sandbox e consome o hook antes do próximo.

    Um `modifier` do trecho anterior não substitui o seguinte: ele já rodou
    e é removido do namespace. Falha de um trecho restaura o payload daquele
    trecho e segue. O caminho de um script só não passa por aqui.
    """
    try:
        ns: dict[str, Any] = {"__builtins__": SAFE_BUILTINS, "math": math}
        if hook_type == "input":
            action = copy.deepcopy(payload) if isinstance(payload, dict) else {}
            ns.update({
                "action": action,
                "text": action.get("text", ""),
                "type": action.get("type", ""),
            })
        elif hook_type == "context":
            ns["context"] = copy.deepcopy(payload) if isinstance(payload, list) else []
        elif hook_type == "output":
            text_str = str(payload) if payload is not None else ""
            ns.update({"text": text_str, "output": text_str})
        else:
            pipe.send(("ok", payload))
            return

        for part in parts:
            before_action = copy.deepcopy(ns.get("action")) if isinstance(ns.get("action"), dict) else None
            before_context = copy.deepcopy(ns.get("context")) if isinstance(ns.get("context"), list) else None
            before_text = ns.get("text") if isinstance(ns.get("text"), str) else ""
            before_output = ns.get("output")
            try:
                exec(compile(part, "<sandboxed_script>", "exec"), ns)
                if hook_type == "input":
                    _finish_input_part(ns, before_action)
                elif hook_type == "context":
                    _finish_context_part(ns)
                elif hook_type == "output":
                    _finish_output_part(ns, before_text)
            except Exception:
                if hook_type == "input" and isinstance(before_action, dict):
                    ns["action"] = before_action
                    ns["text"] = before_action.get("text", "")
                    ns["type"] = before_action.get("type", "")
                elif hook_type == "context" and isinstance(before_context, list):
                    ns["context"] = before_context
                elif hook_type == "output":
                    ns["text"] = before_text
                    if isinstance(before_output, str):
                        ns["output"] = before_output
            finally:
                _pop_hook_fns(ns)

        if hook_type == "input":
            action = ns.get("action")
            pipe.send(("ok", action if isinstance(action, dict) else payload))
        elif hook_type == "context":
            ctx = ns.get("context")
            pipe.send(("ok", ctx if isinstance(ctx, list) else payload))
        elif hook_type == "output":
            text = ns.get("text")
            pipe.send(("ok", text if isinstance(text, str) else payload))
        else:
            pipe.send(("ok", payload))
    except Exception as exc:
        pipe.send(("err", f"{type(exc).__name__}: {exc}"))


def _execute_chain(hook_type: str, parts: list[str], payload: Any, timeout: float) -> Any:
    safe: list[str] = []
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            continue
        try:
            validate_script_ast(part)
        except Exception as exc:
            log.warning("Scripting %s chain part rejected by AST validator: %s", hook_type, exc)
            continue
        safe.append(part)
    if not safe:
        return payload
    if len(safe) == 1:
        return _execute_sandboxed(hook_type, safe[0], payload, timeout)

    effective_timeout = max(0.01, float(timeout if timeout is not None else 1.0))
    mp_ctx = mp.get_context("spawn")
    parent_conn, child_conn = mp_ctx.Pipe(duplex=False)
    proc = mp_ctx.Process(
        target=_sandbox_worker_chain,
        args=(child_conn, hook_type, safe, payload),
    )
    try:
        proc.start()
        child_conn.close()
        proc.join(timeout=effective_timeout)
        if proc.is_alive():
            log.warning("Scripting %s chain timed out after %.2fs", hook_type, effective_timeout)
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
            log.warning("Scripting %s chain execution error: %s", hook_type, res)
            return payload
    except Exception as exc:
        log.warning("Scripting %s chain subprocess error: %s", hook_type, exc)
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


def _execute_sandboxed(hook_type: str, script: str, payload: Any, timeout: float = 1.0) -> Any:
    """Executes script in an isolated subprocess with AST check and timeout.

    Always fail-safe: returns original payload on any failure or timeout.
    """
    if not script or not isinstance(script, str) or not script.strip():
        return payload

    parts = chain_parts(script)
    if parts is not None:
        return _execute_chain(hook_type, parts, payload, timeout)

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

def _adventure_script_key(adventure_id: str, hook: str) -> str:
    return f"script:{adventure_id}:{hook}"


def _global_script_key(hook: str) -> str:
    return f"script:global:{hook}"


def _store_script(conn, key: str, script: str | None) -> None:
    if script and str(script).strip():
        db.set_setting(conn, key, script)
    else:
        db.delete_setting(conn, key)


def adventure_scripts_view(conn, adventure_id: str) -> dict:
    """Textos salvos na aventura, sem executar e sem copiar o global para ela.

    `sources` diz o que o turno vai usar: adventure, global ou empty.
    Fallback global só quando a chave da aventura não existe.
    """
    scripts: dict[str, str] = {}
    sources: dict[str, str] = {}
    global_scripts: dict[str, str] = {}
    for hook in SCRIPT_HOOKS:
        own = db.get_setting(conn, _adventure_script_key(adventure_id, hook))
        glob = db.get_setting(conn, _global_script_key(hook))
        scripts[hook] = own if own is not None else ""
        global_scripts[hook] = glob or ""
        if own is not None and own.strip():
            sources[hook] = "adventure"
        elif own is None and glob and glob.strip():
            sources[hook] = "global"
        else:
            sources[hook] = "empty"
    return {
        "input_modifier": scripts["input_modifier"],
        "context_modifier": scripts["context_modifier"],
        "output_modifier": scripts["output_modifier"],
        "sources": sources,
        "global": global_scripts,
    }


def get_adventure_script(adventure_id: str, hook: str) -> str | None:
    """Script efetivo do hook para o turno.

    Precedência da base, intacta: a chave da aventura, mesmo vazia, bloqueia
    o global; ausência cai no global. Mods habilitados entram depois da base,
    na ordem de aplicação. Mod desabilitado não entra e o texto salvo não é
    reescrito. Sem mod, o retorno é a string base original (ou None).
    """
    try:
        with db.session() as conn:
            own = db.get_setting(conn, _adventure_script_key(adventure_id, hook))
            if own is not None:
                base: str | None = own
            else:
                base = db.get_setting(conn, _global_script_key(hook))
            extra: list[str] = []
            try:
                from app import mods as mod_catalog
                extra = mod_catalog.enabled_hook_scripts(conn, adventure_id, hook)
            except Exception as exc:
                log.warning("mod scripts unavailable for %s %s: %s", adventure_id, hook, exc)
                extra = []
            return compose_hook_scripts(base, extra)
    except Exception as exc:
        log.warning("Could not read script for adventure %s hook %s: %s", adventure_id, hook, exc)
        return None


def set_adventure_script(adventure_id: str, hook: str, script: str) -> None:
    """Grava o hook da aventura. Vazio apaga a chave para o global voltar a valer."""
    with db.session() as conn:
        _store_script(conn, _adventure_script_key(adventure_id, hook), script)


def save_adventure_scripts(adventure_id: str, update: dict) -> dict:
    """Valida pelo AST do sandbox e persiste. Não executa o script.

    Chave ausente ou None não mexe no hook. String vazia apaga a chave da aventura.
    """
    pending: dict[str, str] = {}
    for hook in SCRIPT_HOOKS:
        if hook not in update or update[hook] is None:
            continue
        value = update[hook]
        if not isinstance(value, str):
            raise ValueError(f"{hook} must be a string")
        if value.strip():
            validate_script_ast(value)
        pending[hook] = value
    with db.session() as conn:
        for hook, value in pending.items():
            _store_script(conn, _adventure_script_key(adventure_id, hook), value)
        return adventure_scripts_view(conn, adventure_id)


def read_adventure_scripts(adventure_id: str) -> dict:
    """Lê os scripts da aventura para a API. Não executa."""
    with db.session() as conn:
        return adventure_scripts_view(conn, adventure_id)
