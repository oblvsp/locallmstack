#!/usr/bin/env python3
"""Минимальный агентный цикл на локальной модели: чтение → правка → тест.

Не заменяет pi.dev, а проверяет то, на чём он держится: Ollama отдаёт
tool-calls, приложение исполняет их и возвращает результат, модель
доводит задачу до прохождения тестов.

Песочница создаётся во временном каталоге; исходные файлы проекта
не затрагиваются. Пути инструментов ограничены песочницей.

    python3 agent_loop.py --ctx 8192 --max-steps 6
    python3 agent_loop.py --ctx 16384 --task bug2
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HOST = "http://127.0.0.1:11435"
MODEL = "qwen3.5:9b"

FILES = {
    "calc.py": """def percent(part, whole):
    return part / whole * 100


def average(values):
    return sum(values) / len(values)
""",
    "test_calc.py": """import unittest

from calc import average, percent


class CalcTest(unittest.TestCase):
    def test_percent_normal(self):
        self.assertEqual(percent(25, 200), 12.5)

    def test_percent_zero_whole(self):
        self.assertEqual(percent(0, 0), 0.0)

    def test_average(self):
        self.assertEqual(average([2, 4, 6]), 4.0)


if __name__ == "__main__":
    unittest.main()
""",
}

FILES_BUG2 = {
    "stats.py": """def mean(values):
    return sum(values) / len(values)


def median(values):
    values = sorted(values)
    return values[len(values) // 2]


def report(values):
    return f"mean={mean(values):.2f} median={median(values):.2f}"
""",
    "test_stats.py": """import unittest

from stats import mean, median, report


class StatsTest(unittest.TestCase):
    def test_mean_empty(self):
        self.assertEqual(mean([]), 0.0)

    def test_median_even(self):
        self.assertEqual(median([1, 2, 3, 4]), 2.5)

    def test_report_empty(self):
        self.assertEqual(report([]), "mean=0.00 median=0.00")


if __name__ == "__main__":
    unittest.main()
""",
}

TASKS = {
    "bug1": (
        "В проекте падает тест: percent(0, 0) бросает ZeroDivisionError, "
        "а должен возвращать 0.0. Прочитай calc.py и test_calc.py, исправь только calc.py "
        "и добейся, чтобы все тесты проходили."
    ),
    "bug2": (
        "Тесты в проекте падают: mean([]) и report([]) бросают ZeroDivisionError, "
        "median([1,2,3,4]) возвращает 3 вместо 2.5. Прочитай stats.py и test_stats.py, "
        "исправь только stats.py и добейся, чтобы все тесты проходили. Тесты не меняй."
    ),
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать файл проекта",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Записать файл целиком",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Запустить тесты проекта",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

SYSTEM = (
    "Ты агент в терминале. Работай только инструментами. "
    "Сначала прочитай файлы, потом внеси минимальную правку и запусти тесты. "
    "Не меняй тесты. Когда тесты проходят, ответь одной строкой, что изменил."
)

TASK = (
    "В проекте падает тест: percent(0, 0) бросает ZeroDivisionError, "
    "а должен возвращать 0.0. Прочитай calc.py и test_calc.py, исправь только calc.py "
    "и добейся, чтобы все тесты проходили."
)


class Sandbox:
    def __init__(self, files: dict, test_file: str) -> None:
        self.dir = tempfile.mkdtemp(prefix="locallmstack-sandbox-")
        self.test_file = test_file
        for name, body in files.items():
            with open(os.path.join(self.dir, name), "w") as f:
                f.write(body)

    def path(self, rel: str) -> str:
        p = os.path.realpath(os.path.join(self.dir, rel))
        if not p.startswith(os.path.realpath(self.dir) + os.sep):
            raise ValueError(f"путь вне песочницы: {rel}")
        return p

    def call(self, name: str, args: dict) -> str:
        if name == "read_file":
            with open(self.path(args["path"])) as f:
                return f.read()[:8000]
        if name == "write_file":
            with open(self.path(args["path"]), "w") as f:
                f.write(args["content"])
            return "записано"
        if name == "run_tests":
            r = subprocess.run(
                [sys.executable, "-m", "unittest", "-q", self.test_file],
                cwd=self.dir, capture_output=True, text=True, timeout=120,
            )
            out = (r.stdout + r.stderr).strip()[-1500:]
            return f"exit={r.returncode}\n{out}"
        return f"неизвестный инструмент {name}"

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def chat(messages: list, ctx: int, num_predict: int, think: bool = False) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        "think": think,
        "options": {"num_ctx": ctx, "temperature": 0.1, "num_predict": num_predict},
    }
    req = urllib.request.Request(
        f"{HOST}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=1800).read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--max-steps", type=int, default=6)
    ap.add_argument("--num-predict", type=int, default=700)
    ap.add_argument("--task", choices=sorted(TASKS), default="bug1")
    ap.add_argument("--think", action="store_true")
    args = ap.parse_args()

    scenario = args.task
    box = Sandbox(FILES if scenario == "bug1" else FILES_BUG2,
                  "test_calc.py" if scenario == "bug1" else "test_stats.py")
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TASKS[scenario]},
    ]
    t0 = time.perf_counter()
    steps, passed, final = [], False, ""
    try:
        for step in range(1, args.max_steps + 1):
            t = time.perf_counter()
            r = chat(messages, args.ctx, args.num_predict, args.think)
            msg = r.get("message") or {}
            calls = msg.get("tool_calls") or []
            entry = {
                "step": step,
                "s": round(time.perf_counter() - t, 1),
                "prompt_tok": r.get("prompt_eval_count"),
                "out_tok": r.get("eval_count"),
                "content": (msg.get("content") or "")[:200],
                "tools": [c["function"]["name"] for c in calls],
                "finish": r.get("done_reason"),
            }
            steps.append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)

            if not calls:
                final = msg.get("content") or ""
                break

            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            for c in calls:
                fn = c["function"]
                a = fn.get("arguments")
                if isinstance(a, str):
                    try:
                        a = json.loads(a)
                    except json.JSONDecodeError:
                        a = {}
                try:
                    result = box.call(fn["name"], a or {})
                except Exception as e:  # noqa: BLE001
                    result = f"ошибка инструмента: {e}"
                messages.append({"role": "tool", "tool_name": fn["name"],
                                 "content": result})
            box_ok = subprocess.run(
                [sys.executable, "-m", "unittest", "-q", box.test_file],
                cwd=box.dir, capture_output=True, text=True, timeout=120,
            ).returncode == 0
            if box_ok:
                passed = True
        total = time.perf_counter() - t0
        print(f"\nитог: тесты {'проходят' if passed else 'НЕ проходят'} · "
              f"шагов {len(steps)} · {total:.1f} s · ctx={args.ctx} · {scenario}")
        print("изменённые файлы после цикла:")
        for name in sorted(os.listdir(box.dir)):
            if not name.startswith("test_") and name.endswith(".py"):
                print(f"--- {name} ---")
                with open(os.path.join(box.dir, name)) as f:
                    print(f.read(), end="")
        if final:
            print("финальный ответ модели:", final.strip()[:400])
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               f"loop-{scenario}-ctx{args.ctx}-{int(time.time())}.json"),
                  "w") as f:
            json.dump({"scenario": scenario, "ctx": args.ctx, "think": args.think, "steps": steps,
                       "passed": passed, "final": final}, f, ensure_ascii=False,
                      indent=2)
    finally:
        box.cleanup()
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
