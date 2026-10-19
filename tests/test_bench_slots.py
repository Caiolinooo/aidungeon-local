"""tools/bench_slots.py: mesmo mapa de slots do app (C1) e as metas da seção 2."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

from app import llm_client

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import bench_slots  # noqa: E402


@pytest.mark.parametrize("total", [1, 2, 3, 4, 5])
def test_slot_map_matches_the_app(total):
    llm_client.invalidate_slot_cache()
    cfg = {"base_url": f"http://bench-{total}:1234/v1", "model_alias": "auto",
           "timeout_seconds": 5}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"total_slots": total}))
    for role, slot in bench_slots.slot_map(total).items():
        assert llm_client.slot_for(cfg, role, transport) == slot, (total, role)


def test_targets_report_what_missed():
    bench = bench_slots.Bench(None, bench_slots.build_parser().parse_args([]))
    bench.report = {
        "frio": {"ttft_p50": 2.4, "ttft_p95": 3.9},
        "solo": {"ttft_p50": 0.6, "ttft_p95": 0.9, "gen_tok_s_p50": 18.2},
        "carga": {"aux": ["choices", "chronicle", "image_prompt"], "ttft_p95": 5.9,
                  "gen_tok_s_p50": 12.2, "errors": [], "choices_p50": 11.0,
                  "choices_p95": 13.5},
    }
    missed = [label for label, ok in bench.targets() if not ok]
    assert missed == ["narrativa >= 13 tok/s com 3 auxiliares",
                      "choices p50 <= 6 s com carga", "choices p95 <= 10 s com carga"]
