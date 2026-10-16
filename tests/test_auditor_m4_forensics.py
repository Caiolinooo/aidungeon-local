"""Independent Forensic Audit and Adversarial Stress Test Suite for Milestone 4.

Author: auditor_m4_1 (Forensic Auditor)
Target: Sandboxed Scripting Engine, Story Cards & NPCs Search/Bulk API, DEFECT-CARDS-LLM-LEAK fix.
"""

import ast
import os
import time
import httpx
import pytest

from app import db, deps, scripting
from conftest import make_adventure


def _get_child_pids(parent_pid: int) -> set[int]:
    """Inspect /proc to find all direct child processes of parent_pid."""
    children = set()
    try:
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                pid = int(entry)
                stat_path = f"/proc/{pid}/stat"
                try:
                    with open(stat_path, "r") as f:
                        fields = f.read().split()
                        # Field 4 is ppid
                        if len(fields) > 3 and int(fields[3]) == parent_pid:
                            # Field 3 is state (Z is zombie)
                            state = fields[2]
                            if state != "Z":
                                children.add(pid)
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    pass
    except Exception:
        pass
    return children


# =============================================================================
# 1. AST Visitor & Sandboxing Forensics
# =============================================================================

class TestScriptingASTForensics:
    """Forensic verification that AST visitor is a genuine ast.NodeVisitor and strictly enforces isolation."""

    def test_ast_visitor_is_genuine_subclass(self):
        """Verify SandboxASTValidator is an authentic ast.NodeVisitor and not a facade."""
        assert issubclass(scripting.SandboxASTValidator, ast.NodeVisitor)
        validator = scripting.SandboxASTValidator()
        assert hasattr(validator, "visit_Import")
        assert hasattr(validator, "visit_ImportFrom")
        assert hasattr(validator, "visit_Attribute")
        assert hasattr(validator, "visit_Name")
        assert hasattr(validator, "visit_Constant")

    @pytest.mark.parametrize("payload", [
        # Direct dunder access
        "x = ().__class__",
        "x = ().__class__.__bases__[0]",
        "x = ().__class__.__mro__",
        "x = ().__class__.__subclasses__()",
        # Attribute on custom or built-in object
        "text.__class__",
        "action.__builtins__",
        "action.__globals__",
        "action.__code__",
        # Names starting with dunder
        "__builtins__",
        "__import__('os')",
        # Imports in various forms
        "import os",
        "import sys",
        "import posix",
        "import subprocess",
        "from os import system",
        "from math import __doc__",
        # Dangerous builtins
        "eval('1+1')",
        "exec('x=1')",
        "open('/etc/passwd', 'r')",
        "globals()",
        "locals()",
        "breakpoint()",
        "getattr(action, 'get')",
        "setattr(action, 'k', 'v')",
        "delattr(action, 'k')",
        # Constant string matching dunders
        "k = '__class__'",
        "k = '__subclasses__'",
        "k = '__builtins__'",
    ])
    def test_ast_visitor_rejects_dangerous_payloads(self, payload):
        """AST validator must raise SecurityViolation or fall back safely for all malicious constructs."""
        with pytest.raises((scripting.SecurityViolation, SyntaxError)):
            scripting.validate_script_ast(payload)

        # In runtime hooks, execution must safely fallback without throwing
        initial = {"type": "do", "text": "initial"}
        assert scripting.run_input_modifier(payload, initial) == initial

    def test_valid_script_execution_without_privilege(self):
        """Valid non-malicious logic performs genuine mutations."""
        script = """
action["text"] = "Transformed by logic: " + str(math.sqrt(16))
"""
        initial = {"type": "do", "text": "test"}
        res = scripting.run_input_modifier(script, initial)
        assert res["text"] == "Transformed by logic: 4.0"

    def test_invalid_return_types_fallback_safely(self):
        """If a script returns an unexpected type (e.g. integer instead of dict), fallback to original."""
        script = """
def modifier(act):
    return 12345
"""
        initial = {"type": "do", "text": "test"}
        assert scripting.run_input_modifier(script, initial) == initial

        script_ctx = """
def modifier(ctx):
    return "not a list"
"""
        initial_ctx = [{"role": "system", "content": "hi"}]
        assert scripting.run_context_modifier(script_ctx, initial_ctx) == initial_ctx

        script_out = """
def modifier(text):
    return ["not", "a", "str"]
"""
        initial_out = "hello"
        assert scripting.run_output_modifier(script_out, initial_out) == initial_out


# =============================================================================
# 2. Process Termination & Timeout Forensics
# =============================================================================

class TestProcessTimeoutForensics:
    """Forensic verification that process timeouts actively terminate and kill child processes."""

    def test_infinite_loop_process_killed_no_zombies(self):
        """Verify that an infinite loop child process is killed and doesn't remain alive."""
        parent_pid = os.getpid()
        children_before = _get_child_pids(parent_pid)

        infinite_loop = """
while True:
    x = 1
"""
        t0 = time.time()
        res = scripting.run_input_modifier(infinite_loop, {"type": "do", "text": "safe"}, timeout=0.15)
        elapsed = time.time() - t0

        assert elapsed < 0.8, f"Timeout took too long: {elapsed}s"
        assert res == {"type": "do", "text": "safe"}

        time.sleep(0.1)
        children_after = _get_child_pids(parent_pid)
        # Any new active children spawned should have terminated
        new_active_children = children_after - children_before
        assert len(new_active_children) == 0, f"Child processes leaked: {new_active_children}"

    def test_output_modifier_memory_exhaustion_or_timeout(self):
        """A script allocating huge memory or running heavy loops is bounded by timeout."""
        heavy_script = """
s = "A"
while True:
    s = s + s
"""
        initial = "sample output"
        t0 = time.time()
        res = scripting.run_output_modifier(heavy_script, initial, timeout=0.15)
        elapsed = time.time() - t0

        assert elapsed < 0.8
        assert res == initial


# =============================================================================
# 3. SQL Bulk Operations & Adventure Isolation Forensics
# =============================================================================

class TestBulkSQLIsolationForensics:
    """Forensic verification of parameterized queries and strict multi-tenant isolation."""

    def test_bulk_sql_injection_resilience(self, client):
        """Malicious SQL strings in card_ids must not cause SQL injection or syntax error."""
        adv = make_adventure(title="SQL Injection Adv")
        with db.session() as conn:
            c = db.insert_card(conn, adv["id"], title="Target Card", type="item")

        malicious_ids = [
            c["id"],
            "'; DROP TABLE story_cards; --",
            "' OR '1'='1",
            "nonexistent-id",
        ]

        resp = client.post(
            f"/api/adventures/{adv['id']}/cards/bulk",
            json={"action": "pin", "card_ids": malicious_ids},
        )
        assert resp.status_code == 200
        # Exactly 1 card updated (c["id"])
        assert resp.json()["updated"] == 1

        # Verify story_cards table is intact
        with db.session() as conn:
            assert db.get_card(conn, adv["id"], c["id"])["pinned"] is True

    def test_strict_cross_adventure_isolation_on_all_bulk_actions(self, client):
        """Verify cross-adventure isolation holds for delete, pin, unpin, and retype."""
        adv_victim = make_adventure(title="Victim Adv")
        adv_attacker = make_adventure(title="Attacker Adv")

        with db.session() as conn:
            c_victim1 = db.insert_card(conn, adv_victim["id"], title="V1", type="character", pinned=True)
            c_victim2 = db.insert_card(conn, adv_victim["id"], title="V2", type="location", pinned=False)
            c_attacker = db.insert_card(conn, adv_attacker["id"], title="A1", type="item", pinned=False)

        # Attacker tries to delete victim's cards
        resp = client.post(
            f"/api/adventures/{adv_attacker['id']}/cards/bulk",
            json={"action": "delete", "card_ids": [c_victim1["id"], c_attacker["id"]]},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1  # Only attacker's own card deleted

        with db.session() as conn:
            assert db.get_card(conn, adv_victim["id"], c_victim1["id"]) is not None
            assert db.get_card(conn, adv_attacker["id"], c_attacker["id"]) is None

        # Attacker tries to unpin victim's card
        resp = client.post(
            f"/api/adventures/{adv_attacker['id']}/cards/bulk",
            json={"action": "unpin", "card_ids": [c_victim1["id"]]},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 0

        with db.session() as conn:
            assert db.get_card(conn, adv_victim["id"], c_victim1["id"])["pinned"] is True

        # Attacker tries to retype victim's card
        resp = client.post(
            f"/api/adventures/{adv_attacker['id']}/cards/bulk",
            json={"action": "retype", "card_ids": [c_victim1["id"]], "new_type": "hacked"},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 0

        with db.session() as conn:
            assert db.get_card(conn, adv_victim["id"], c_victim1["id"])["type"] == "character"


# =============================================================================
# 4. DEFECT-CARDS-LLM-LEAK Defect Verification
# =============================================================================

class TestDefectCardsLlmLeakForensics:
    """Forensic verification that unhandled LLM exceptions do not leak as 500 errors."""

    def test_enrich_llm_timeout_handled_as_503(self, client):
        """Enrich card when LLM times out returns 503 instead of 500."""
        adv = make_adventure(title="Timeout Adv")
        with db.session() as conn:
            c = db.insert_card(conn, adv["id"], title="Timeout NPC", entry="NPC waiting.")

        def timeout_transport(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Read timed out from LLM server")

        transport = httpx.MockTransport(timeout_transport)
        client.app.dependency_overrides[deps.get_http_transport] = lambda: transport
        try:
            resp = client.post(f"/api/adventures/{adv['id']}/cards/{c['id']}/enrich")
            assert resp.status_code == 503
            data = resp.json()
            assert data["error"]["code"] == "llm_unreachable"
            assert "LLM service unavailable" in data["error"]["message"]
        finally:
            client.app.dependency_overrides.pop(deps.get_http_transport, None)
