#!/usr/bin/env python3
"""Один запрос к локальной модели через bastion с замером времени.

    python3 ask_local.py --file ../README.md --task task.txt --out answer.md
    python3 ask_local.py "короткий вопрос"

Печатает в stderr модель, контекст и тайминги; ответ — в stdout или --out.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

HOST = "http://127.0.0.1:11434"
MODEL = "qwen3.5:9b"
NUM_CTX = 24576


def ask(prompt: str, system: str | None = None, num_ctx: int = NUM_CTX,
        temperature: float = 0.2, num_predict: int = 900) -> dict:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    payload = {
        "model": MODEL,
        "messages": msgs,
        "stream": False,
        "think": False,
        "options": {"num_ctx": num_ctx, "temperature": temperature,
                    "num_predict": num_predict},
    }
    t0 = time.perf_counter()
    req = urllib.request.Request(
        f"{HOST}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    r = json.loads(urllib.request.urlopen(req, timeout=1800).read())
    r["wall_s"] = time.perf_counter() - t0
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default="")
    ap.add_argument("--file", help="подмешать содержимое файла перед промптом")
    ap.add_argument("--task", help="файл с инструкцией")
    ap.add_argument("--system")
    ap.add_argument("--num-ctx", type=int, default=NUM_CTX)
    ap.add_argument("--num-predict", type=int, default=900)
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--out")
    args = ap.parse_args()

    parts = []
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            parts.append(f.read())
    if args.task:
        with open(args.task, encoding="utf-8") as f:
            parts.append(f.read())
    if args.prompt:
        parts.append(args.prompt)
    if not parts:
        ap.error("нужен промпт, --file или --task")

    r = ask("\n\n".join(parts), args.system, args.num_ctx,
            args.temperature, args.num_predict)
    msg = r.get("message", {})
    text = msg.get("content") or ""
    if msg.get("thinking"):
        print(f"[thinking {len(msg['thinking'])} симв.]", file=sys.stderr)
    ev, ens = r.get("eval_count", 0), r.get("eval_duration", 1) / 1e9
    print(f"[{MODEL} · ctx={args.num_ctx} · prompt={r.get('prompt_eval_count')} tok · "
          f"out={ev} tok · {ev / ens:.1f} tok/s · {r['wall_s']:.1f} s · "
          f"finish={r.get('done_reason')}]", file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
