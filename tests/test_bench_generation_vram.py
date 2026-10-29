"""tools/replay_turn.py fases e tools/bench_generation_vram.py (mocks, sem rede)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import bench_generation_vram as bench  # noqa: E402
import replay_turn  # noqa: E402


NVIDIA = "18100, 4466\n1200, 800\n"
MEMINFO = """MemTotal:       32000000 kB
MemAvailable:    8388608 kB
SwapTotal:       4194304 kB
SwapFree:        2097152 kB
"""


def test_parse_nvidia_and_meminfo():
    assert bench.parse_nvidia_smi(NVIDIA) == (19300, 5266)
    available, swap = bench.parse_meminfo(MEMINFO)
    assert available == 8192
    assert swap == 2048
    with pytest.raises(ValueError):
        bench.parse_nvidia_smi("nope")
    with pytest.raises(ValueError):
        bench.parse_meminfo("MemAvailable: 1 kB\n")


class _Proc:
    def __init__(self, stdout: str, code: int = 0) -> None:
        self.stdout = stdout
        self.returncode = code


def _sampler(samples: list[tuple[str, str]]) -> bench.MemorySampler:
    pending = list(samples)

    def runner(*_args, **_kwargs):
        text, _mem = pending.pop(0)
        return _Proc(text)

    def reader():
        return pending[0][1] if pending else samples[-1][1]

    sampler = bench.MemorySampler(250, runner=runner, meminfo_reader=reader)
    return sampler


def test_sampler_records_min_free_and_swap_delta():
    tight = "20000, 500\n"
    loose = "10000, 4000\n"
    high_swap = MEMINFO.replace("SwapFree:        2097152 kB", "SwapFree:        1048576 kB")
    sampler = _sampler([(NVIDIA, MEMINFO), (tight, high_swap), (loose, MEMINFO)])
    sampler.sample_once()
    sampler.sample_once()
    sampler.sample_once()
    report = bench.metrics_from(sampler, started := __import__("time").monotonic(),
                                {"status": "done"})
    assert report["vram_used_max_mib"] == 20000
    assert report["vram_free_min_mib"] == 500
    assert report["ram_available_min_mib"] == 8192
    assert report["swap_used_delta_mib"] == -1024
    assert report["duration_s"] >= 0
    del started


def test_replay_collects_phase_ends_only(monkeypatch):
    sse = (
        "event: turn_phase\n"
        'data: {"phase":"intent","state":"start"}\n\n'
        "event: token\n"
        'data: {"text":"segredo do prompt"}\n\n'
        "event: turn_phase\n"
        'data: {"phase":"intent","state":"end","elapsed_ms":1500}\n\n'
        "event: turn_phase\n"
        'data: {"phase":"narrative_wait","state":"end","elapsed_ms":400,"note":"drop me"}\n\n'
        "event: done\n"
        'data: {"action":{"seq":3,"text":"prosa longa"}}\n\n'
    )

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def __iter__(self):
            return iter(line.encode() for line in io.StringIO(sse))

    api = replay_turn.Api("http://example", "secret-token", 5)
    monkeypatch.setattr(api.opener, "open", lambda *_a, **_k: _Resp())
    result = api.stream_action("adv", "do", "secret action text")
    assert result["phases"] == [
        {"phase": "intent", "elapsed_ms": 1500},
        {"phase": "narrative_wait", "elapsed_ms": 400},
    ]
    assert "prompt" not in json.dumps(result["phases"])
    assert result["seq"] == 3
    assert result["tokens"] == 1

    args = replay_turn.build_parser().parse_args(
        ["--adventure", "adv", "--turn", "do:olho"])
    replay = replay_turn.Replay(api, args)
    replay.pool.shutdown(wait=False)


def test_replay_run_aggregates_phase_seconds(monkeypatch, capsys):
    args = replay_turn.build_parser().parse_args(
        ["--adventure", "adv", "--turn", "do:olho", "--jobs-wait", "0"])
    calls = {"n": 0}

    def call(method, path, body=None):
        calls["n"] += 1
        if path.endswith("/adv") and method == "GET" and calls["n"] == 1:
            return {"status": 200, "body": {"title": "t", "actions": [], "story_cards": []},
                    "error": None, "elapsed_s": 0.01}
        return {"status": 200, "body": [], "error": None, "elapsed_s": 0.01}

    api = replay_turn.Api("http://example", "t", 5)
    monkeypatch.setattr(api, "call", call)
    monkeypatch.setattr(api, "stream_action", lambda *_a, **_k: {
        "status": 200, "ttft_s": 0.2, "total_s": 1.0, "tokens": 1, "seq": 1,
        "error": None, "phases": [
            {"phase": "action_check", "elapsed_ms": 250},
            {"phase": "npc_gate", "elapsed_ms": 750},
        ],
    })
    replay = replay_turn.Replay(api, args)
    assert replay.run() == 0
    assert replay.stats["phase:action_check"] == [0.25]
    assert replay.stats["phase:npc_gate"] == [0.75]
    out = capsys.readouterr().out
    assert "phase:action_check" in out
    assert "phase:npc_gate" in out


def test_poll_stops_on_terminal_error():
    api = replay_turn.Api("http://example", "t", 5)
    seen = {"n": 0}

    def call(method, path, body=None):
        seen["n"] += 1
        if seen["n"] == 1:
            return {"status": 200, "body": {"id": "j", "status": "running"}, "error": None}
        return {"status": 200, "body": {"id": "j", "status": "error", "error": "job exceeded",
                                         "result": {"code": "job_timeout"}},
                "error": None}

    api.call = call
    view = bench.poll_job(api, "adv", "j", timeout_s=2)
    assert view["status"] == "error"
    assert seen["n"] == 2


def test_portrait_and_clip_contracts(monkeypatch):
    posts: list[tuple[str, dict | None]] = []

    def call(method, path, body=None):
        if method == "POST":
            posts.append((path, body))
            if path.endswith("/portrait"):
                return {"status": 202, "body": {"id": "jobp", "status": "pending"}, "error": None}
            return {"status": 202, "body": {"job_id": "jobc"}, "error": None}
        return {"status": 200, "body": {"status": "done", "result": {"width": 848, "height": 816}},
                "error": None}

    api = replay_turn.Api("http://example", "t", 5)
    api.call = call
    portrait = bench.run_portrait(api, "adv", "card1", 2)
    clip = bench.run_clip(api, "adv", "img1", 2)
    assert portrait["status"] == "done"
    assert portrait["width"] == 848 and portrait["height"] == 816
    assert posts[0][0].endswith("/cards/card1/portrait")
    assert posts[1][0].endswith("/clips")
    assert posts[1][1]["image_id"] == "img1"
    assert posts[1][1]["prompt"].startswith(bench.CLIP_PROMPT)
    assert "secret" not in posts[1][1]["prompt"]
    assert clip["status"] == "done"


def test_flux_initializes_real_database_before_reading_settings(tmp_db, monkeypatch):
    from app import image_client

    monkeypatch.setattr(image_client, "_generate_comfyui",
                        lambda *_args: (b"png", 1024, 1024))
    result = bench.run_flux(5)
    assert result == {"status": "done", "error": None, "width": 1024,
                      "height": 1024, "bytes": 3}


def test_failed_job_exits_one_even_without_memory_samples():
    report = {"status": "error", "error": "job_timeout", "vram_free_min_mib": None}
    assert bench.classify(report, "no memory samples") == 1
    assert report["error"] == "job_timeout"


def test_cli_exit_codes(monkeypatch, tmp_path, capsys):
    def fake_flux(_timeout):
        return {"status": "done", "error": None, "width": 1024, "height": 1024, "bytes": 12}

    monkeypatch.setattr(bench, "run_flux", fake_flux)

    class Quiet(bench.MemorySampler):
        def start(self):
            self.vram_used = [1000]
            self.vram_free = [1200]
            self.ram_available = [4000]
            self.swap_used = [10, 12]

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(bench, "MemorySampler", Quiet)
    out = tmp_path / "ok.json"
    code = bench.main(["--adventure", "adv", "--mode", "flux", "--output", str(out)])
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(out.read_text())
    assert printed == saved
    assert printed["vram_free_min_mib"] == 1200
    assert printed["bytes"] == 12
    assert printed["swap_used_delta_mib"] == 2
    assert "prompt" not in json.dumps(printed)

    class Tight(Quiet):
        def start(self):
            super().start()
            self.vram_free = [899]

    monkeypatch.setattr(bench, "MemorySampler", Tight)
    assert bench.main(["--adventure", "adv", "--mode", "flux"]) == 1
    capsys.readouterr()

    def fail(_timeout):
        return {"status": "error", "error": "Bearer sk-secret prompt leaked"}

    monkeypatch.setattr(bench, "run_flux", fail)
    monkeypatch.setattr(bench, "MemorySampler", Quiet)
    code = bench.main(["--adventure", "adv", "--mode", "flux"])
    body = json.loads(capsys.readouterr().out)
    assert code == 1
    assert body["error"] == "generation failed"
    assert "secret" not in body["error"]

    assert bench.main(["--adventure", "adv", "--mode", "portrait"]) == 2
    assert "requires --card" in capsys.readouterr().out
    assert bench.main(["--adventure", "adv", "--mode", "clip"]) == 2
    capsys.readouterr()

    class Broken(Quiet):
        def start(self):
            self.error = "nvidia-smi falhou"

    monkeypatch.setattr(bench, "run_flux", fake_flux)
    monkeypatch.setattr(bench, "MemorySampler", Broken)
    assert bench.main(["--adventure", "adv", "--mode", "flux"]) == 2
