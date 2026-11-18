"""Check GPU priority, memory verification, and release-and-retry without touching Colab."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workbench"))
import gpu

CARDS = {"A100-40": "NVIDIA A100-SXM4-40GB, 40960", "A100-80": "NVIDIA A100-SXM4-80GB, 81920",
         "G4": "NVIDIA RTX PRO 6000 Blackwell Server Edition, 97887", "H100": "NVIDIA H100 80GB HBM3, 81559"}


def scenario(plan, offers):
    """offers: per allocation attempt, None (refused) or a CARDS key."""
    calls, queue, current = [], list(offers), {}

    def run(argv, timeout):
        calls.append(" ".join(argv[3:]))
        current["card"] = queue.pop(0)
        return current["card"] is not None

    def release():
        calls.append("stop")
        return True

    result = gpu.acquire("s", plan, run, lambda: CARDS[current["card"]], release,
                         say=lambda _: None, sleep=lambda _: None)
    return result, calls


# A 40 GB A100 is released and the next round gets an 80 GB card.
result, calls = scenario({"gpus": ["A100"], "rounds": 3, "wait": 10}, ["A100-40", "A100-80"])
assert result["gpu"] == "A100" and result["mib"] == 81920, result
assert calls == ["--gpu A100 --high-mem", "stop", "--gpu A100 --high-mem"], calls

# Priority order falls through to the next accelerator when the first one is refused.
result, calls = scenario({"gpus": ["G4", "H100"], "rounds": 1, "wait": 10}, [None, "H100"])
assert result["gpu"] == "H100" and calls == ["--gpu G4", "stop", "--gpu H100 --high-mem"], calls

# Exhausting every round raises after releasing each wrong card.
try:
    scenario({"gpus": ["A100"], "rounds": 2, "wait": 10, "high_mem": False}, ["A100-40", "A100-40"])
except RuntimeError as error:
    assert "2 次" in str(error)
else:
    raise AssertionError("expected failure")

for bad in ({"gpus": ["T4"]}, {"gpus": []}, {"gpus": ["G4", "G4"]}, {"rounds": 0}, {"wait": 5}):
    try:
        gpu.normalize(bad)
    except ValueError:
        continue
    raise AssertionError(bad)
assert gpu.parse_smi("Warning: banner\nNVIDIA A100-SXM4-80GB, 81920\n") == ("NVIDIA A100-SXM4-80GB", 81920)
print("GPU PASS: priority order, A100 80 GB verification, release and retry, plan validation.")
