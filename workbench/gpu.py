#!/usr/bin/env python3
"""Acquire a Colab GPU from a priority list, verify its memory, and retry when the card is wrong.

The H3 Ref2VA renderer peaks at about 74 GiB for a six-second shot, so only 80 GB and 96 GB class
cards can run it. Colab hands out both 40 GB and 80 GB A100s under the same accelerator name, so
every allocation is checked with nvidia-smi and released again when it does not meet the minimum.
"""
import argparse
import re
import subprocess
import sys
import time

# Minimum memory.total reported by nvidia-smi, in MiB. 80 GB cards report 81 559–81 920 MiB.
GPUS = {
    "G4": {"label": "G4 · RTX PRO 6000 96GB", "min_mib": 90_000, "high_mem": False},
    "H100": {"label": "H100 · 80GB", "min_mib": 79_000, "high_mem": True},
    "A100": {"label": "A100 · 只要 80GB", "min_mib": 79_000, "high_mem": True},
}
DEFAULT_PLAN = {"gpus": ["G4"], "rounds": 3, "wait": 45, "high_mem": True}
# Voice synthesis (Qwen3-TTS 1.7B) fits on small cards, so it never competes for the render GPUs.
TTS_GPUS = {
    "L4": {"label": "L4 · 24GB", "min_mib": 20_000, "high_mem": False},
    "T4": {"label": "T4 · 16GB", "min_mib": 14_000, "high_mem": False},
}
TTS_PLAN = {"gpus": ["L4", "T4"], "rounds": 3, "wait": 30, "high_mem": False}
LINE = re.compile(r"^\s*([^,\n]+?)\s*,\s*(\d+)\s*$", re.M)


class Cancelled(Exception):
    pass


def normalize(plan, table=GPUS):
    """Validate a user supplied plan and fill defaults."""
    plan = dict(DEFAULT_PLAN if table is GPUS else TTS_PLAN, **(plan or {}))
    gpus = plan["gpus"]
    if not isinstance(gpus, list) or not 1 <= len(gpus) <= len(table) or len(set(gpus)) != len(gpus) \
            or any(gpu not in table for gpu in gpus):
        raise ValueError("请选择可用显卡：" + "、".join(spec["label"] for spec in table.values()) + "。")
    rounds, wait = plan["rounds"], plan["wait"]
    if type(rounds) is not int or not 1 <= rounds <= 30:
        raise ValueError("抢卡轮数须在 1–30 之间。")
    if type(wait) is not int or not 10 <= wait <= 600:
        raise ValueError("每轮间隔须在 10–600 秒之间。")
    if type(plan["high_mem"]) is not bool:
        raise ValueError("高内存选项无效。")
    return {"gpus": gpus, "rounds": rounds, "wait": wait, "high_mem": plan["high_mem"]}


def parse_smi(output):
    match = LINE.findall(output)
    if not match:
        return None
    name, mib = match[-1]
    return name.strip(), int(mib)


def acquire(session, plan, run, probe, release, cancelled=lambda: False, say=print, sleep=time.sleep, table=GPUS):
    """Allocate one GPU that satisfies the plan.

    run(argv, timeout) -> bool          allocate with the Colab CLI (True on success)
    probe() -> str                       nvidia-smi output from the new runtime
    release() -> bool                    stop the runtime (True when confirmed)
    Returns {"gpu", "name", "mib", "attempt"}. Raises RuntimeError when every attempt failed, and
    Cancelled between attempts. A failed release is fatal because the runtime is still billed.
    """
    plan = normalize(plan, table)
    attempt = 0
    for round_no in range(1, plan["rounds"] + 1):
        for gpu in plan["gpus"]:
            if cancelled():
                raise Cancelled("任务已取消")
            attempt += 1
            spec = table[gpu]
            argv = ["new", "-s", session, "--gpu", gpu]
            if spec["high_mem"] and plan["high_mem"]:
                argv.append("--high-mem")
            say(f"[抢卡] 第 {round_no}/{plan['rounds']} 轮 · 尝试 {spec['label']}")
            allocated = run(argv, 150)
            if not allocated:
                say(f"[抢卡] {gpu} 暂无可分配资源")
                # A timed-out request may still have created the assignment.
                if not release():
                    raise RuntimeError(f"{gpu} 申请失败后无法确认释放，请在任务区释放 GPU。")
                continue
            found = None
            for _ in range(3):
                found = parse_smi(probe() or "")
                if found:
                    break
                sleep(5)
            if not found:
                say(f"[抢卡] {gpu} 已分配但无法读取显卡信息，释放后重试")
            else:
                name, mib = found
                say(f"[抢卡] 分到 {name} · {mib / 1024:.1f} GiB 显存")
                if mib >= spec["min_mib"]:
                    return {"gpu": gpu, "name": name, "mib": mib, "attempt": attempt}
                say(f"[抢卡] 显存低于要求的 {spec['min_mib'] / 1024:.0f} GiB，释放后重新抢")
            if not release():
                raise RuntimeError(f"释放不合格的 {gpu} 失败，请在任务区释放 GPU。")
        if round_no < plan["rounds"]:
            say(f"[抢卡] 本轮未抢到合适显卡，{plan['wait']} 秒后重试")
            deadline = time.monotonic() + plan["wait"]
            while time.monotonic() < deadline:
                if cancelled():
                    raise Cancelled("任务已取消")
                sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"尝试 {attempt} 次仍未抢到合适显卡（{'、'.join(plan['gpus'])}）。稍后再试，或增加候选显卡。")


SMI = ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--cli", default="colab")
    parser.add_argument("--identity")
    parser.add_argument("--gpus", default="G4", help="Comma separated priority list, e.g. G4,H100,A100")
    parser.add_argument("--rounds", type=int, default=DEFAULT_PLAN["rounds"])
    parser.add_argument("--wait", type=int, default=DEFAULT_PLAN["wait"])
    parser.add_argument("--no-high-mem", action="store_true")
    args = parser.parse_args()
    identity = ["-i", args.identity] if args.identity else []

    def run(argv, timeout):
        try:
            return subprocess.run([args.cli, *argv], timeout=timeout, stdin=subprocess.DEVNULL).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def probe():
        try:
            result = subprocess.run([args.cli, "ssh", "-s", args.session, *identity, "--", *SMI],
                                    capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
            return result.stdout
        except subprocess.TimeoutExpired:
            return ""

    def release():
        try:
            return subprocess.run([args.cli, "stop", "-s", args.session], timeout=60,
                                  stdin=subprocess.DEVNULL).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    plan = {"gpus": [g.strip().upper() for g in args.gpus.split(",") if g.strip()], "rounds": args.rounds,
            "wait": args.wait, "high_mem": not args.no_high_mem}
    try:
        result = acquire(args.session, plan, run, probe, release, say=lambda text: print(text, flush=True))
    except (ValueError, RuntimeError, Cancelled) as error:
        print(f"GPU allocation failed: {error}", file=sys.stderr)
        return 1
    print(f"GPU ready: {result['gpu']} {result['name']} {result['mib']} MiB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
