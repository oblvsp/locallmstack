#!/usr/bin/env python3
"""Минимальный агентный цикл с принудительной внешней проверкой.

Модель работает только через инструменты read/write/edit/list/run_tests.
Запись разрешена лишь в пути из --allow. После каждого шага обвязка сама
запускает тесты, а завершение принимает только при выполнении всех условий:

  1. тесты зелёные;
  2. дифф непустой и не выходит за --allow;
  3. необязательная скрытая приёмка --check-cmd тоже зелёная.

Слово модели «готово» ничего не значит: без прохождения гейта цикл
продолжается, а по исчерпании шагов прогон считается проваленным.

    python3 bench/verify_loop.py \
      --repo /tmp/project --allow calc.py \
      --test-cmd "python3 -m unittest -q test_calc.py" \
      --task "Исправь percent(0, 0) и добейся зелёных тестов."

Репозиторий должен быть чистым: цикл не должен смешивать свои правки
с чужими. Скрытая приёмка не показывается модели. После --repeat-limit
одинаковых вызовов подряд цикл останавливается как застрявший.
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST = "http://127.0.0.1:11435"
MODEL = "qwen3.5:9b"
IGNORED = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/")
IGNORED_EXT = (".pyc", ".pyo")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Список файлов репозитория",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "подкаталог, по умолчанию ."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать файл по строкам",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "первая строка, с 1"},
                    "limit": {"type": "integer", "description": "сколько строк, по умолчанию 250"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Заменить ровно одно вхождение old на new",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
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
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": "Запустить тесты проекта (обвязка также запускает их сама)",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class ToolError(Exception):
    pass


class Repo:
    def __init__(self, root: str, allow: list[str]) -> None:
        self.root = os.path.realpath(root)
        self.allow = [a.strip().rstrip("/") for a in allow if a.strip()]

    def rel(self, path: str) -> str:
        p = os.path.realpath(os.path.join(self.root, path))
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ToolError(f"путь вне репозитория: {path}")
        return os.path.relpath(p, self.root).replace(os.sep, "/")

    def writable(self, path: str) -> None:
        if not any(
            fnmatch.fnmatch(path, a) or path == a or path.startswith(a + "/")
            for a in self.allow
        ):
            raise ToolError(f"запись в {path} запрещена; разрешено: {', '.join(self.allow)}")

    def read(self, path: str, offset: int = 1, limit: int = 250) -> str:
        rel = self.rel(path)
        full = os.path.join(self.root, rel)
        if not os.path.isfile(full):
            raise ToolError(f"нет файла {rel}")
        with open(full, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        start = max(1, offset)
        chunk = lines[start - 1 : start - 1 + max(1, min(limit, 400))]
        body = "".join(chunk)
        if len(body) > 14000:
            body = body[:14000] + "\n... [обрезано, читай частями]"
        return f"{rel}: строки {start}–{start + len(chunk) - 1} из {len(lines)}\n{body}"

    def list_files(self, path: str = ".") -> str:
        base = os.path.join(self.root, self.rel(path))
        if not os.path.isdir(base):
            raise ToolError(f"нет каталога {path}")
        out = []
        for cur, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "node_modules", ".venv"}]
            for name in sorted(files):
                rel = os.path.relpath(os.path.join(cur, name), self.root).replace(os.sep, "/")
                out.append(rel)
                if len(out) >= 300:
                    return "\n".join(out + ["... [список обрезан]"])
        return "\n".join(out)

    def edit(self, path: str, old: str, new: str) -> str:
        rel = self.rel(path)
        self.writable(rel)
        full = os.path.join(self.root, rel)
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
        n = text.count(old)
        if n != 1:
            raise ToolError(f"old встречается {n} раз; нужна ровно одна замена")
        updated = text.replace(old, new, 1)
        with open(full, "w", encoding="utf-8") as f:
            f.write(updated)
        diff = "".join(difflib.unified_diff(
            text.splitlines(keepends=True), updated.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=2,
        ))
        if len(diff) > 2000:
            diff = diff[:2000] + "\n... [дифф обрезан]"
        return f"заменено в {rel}\n{diff}"

    def write(self, path: str, content: str) -> str:
        rel = self.rel(path)
        self.writable(rel)
        os.makedirs(os.path.dirname(os.path.join(self.root, rel)) or self.root, exist_ok=True)
        with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
            f.write(content)
        return f"записан {rel} ({len(content)} символов)"

    def run(self, cmd: str, timeout: int) -> tuple[int, str, float]:
        t0 = time.perf_counter()
        try:
            r = subprocess.run(
                cmd, shell=True, cwd=self.root, capture_output=True,
                text=True, timeout=timeout,
            )
            code, out = r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            code, out = 124, f"таймаут {timeout}s"
        return code, out, time.perf_counter() - t0

    def changed(self) -> list[str]:
        r = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.root,
            capture_output=True, text=True, check=True,
        )
        paths = []
        for line in r.stdout.splitlines():
            p = line[3:].strip()
            if " -> " in p:
                p = p.split(" -> ", 1)[1]
            p = p.strip('"')
            if p.endswith(IGNORED_EXT) or any(p.startswith(i) for i in IGNORED):
                continue
            paths.append(p)
        return sorted(set(paths))

    def diff_stat(self) -> str:
        r = subprocess.run(
            ["git", "diff", "--stat"], cwd=self.root,
            capture_output=True, text=True,
        )
        return r.stdout.strip() or "(нет изменений в отслеживаемых файлах)"


def chat(messages: list, ctx: int, num_predict: int, think: bool, model: str,
         host: str) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "stream": False,
        "think": think,
        "options": {"num_ctx": ctx, "temperature": 0.1, "num_predict": num_predict},
    }
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=1800).read())


def brief_args(args: dict) -> dict:
    def short(value):
        if value is None or isinstance(value, (int, float, bool)):
            return value
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return s if len(s) <= 160 else s[:160] + "…"
    return {k: short(v) for k, v in (args or {}).items()}


def calls_signature(parsed: list[tuple[str, dict]]) -> str:
    return json.dumps(parsed, ensure_ascii=False, sort_keys=True)


def estimate_tokens(messages: list) -> int:
    return int(sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) / 3.2) + 1200


def shrink(messages: list, budget: int) -> int:
    """Сокращает старые выводы инструментов, не ломая пары tool_call → tool."""
    changed = 0
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-4]:
        if estimate_tokens(messages) <= budget:
            break
        body = str(messages[i].get("content") or "")
        if len(body) <= 500:
            continue
        messages[i]["content"] = body[:500] + "\n... [вывод сокращён; перечитай при необходимости]"
        changed += 1
    return changed


def system_prompt(allow: list[str]) -> str:
    return (
        "Ты агент в репозитории. Работай только инструментами. "
        f"Менять файлы можно только здесь: {', '.join(allow)}. Остальное — только чтение.\n"
        "Порядок: найди нужные файлы (list_files), прочитай их, внеси минимальную правку, "
        "запусти run_tests. Если тесты падают — читай вывод и исправляй.\n"
        "Обвязка сама запускает тесты после каждого шага и сама проверяет дифф. "
        "Твоё «готово» не принимается: цикл закончится только по зелёным тестам и чистому диффу. "
        "Не меняй тесты и не пытайся обойти проверку. "
        "Если повторяешь один и тот же вызов, результат не изменится — смени подход."
    )


def tool_result(repo: Repo, name: str, args: dict, test_cmd: str, timeout: int) -> str:
    try:
        if name == "list_files":
            return repo.list_files(args.get("path") or ".")
        if name == "read_file":
            return repo.read(args["path"], int(args.get("offset") or 1), int(args.get("limit") or 250))
        if name == "edit_file":
            return repo.edit(args["path"], args["old"], args["new"])
        if name == "write_file":
            return repo.write(args["path"], args["content"])
        if name == "run_tests":
            code, out, _ = repo.run(test_cmd, timeout)
            return f"exit={code}\n{out[-4000:]}"
        return f"неизвестный инструмент {name}"
    except (ToolError, KeyError, OSError) as e:
        return f"ошибка: {e}"


def gate(repo: Repo, allow: list[str], test_cmd: str, check_cmd: str | None,
         timeout: int) -> dict:
    code, out, secs = repo.run(test_cmd, timeout)
    changed = repo.changed()
    violations = [
        p for p in changed
        if not any(fnmatch.fnmatch(p, a) or p == a or p.startswith(a + "/") for a in allow)
    ]
    check_code, check_out = None, ""
    if check_cmd and changed and not violations:
        check_code, check_out, _ = repo.run(check_cmd, timeout)
    tests_ok = code == 0
    diff_ok = bool(changed)
    check_ok = None if check_code is None else check_code == 0
    return {
        "tests_ok": tests_ok,
        "tests_exit": code,
        "tests_secs": round(secs, 2),
        "tests_tail": out[-3000:],
        "changed": changed,
        "violations": violations,
        "diff_nonempty": diff_ok,
        "check_ok": check_ok,
        "check_exit": check_code,
        "check_tail": check_out[-2000:],
        "passed": tests_ok and diff_ok and not violations and check_ok is not False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--task")
    ap.add_argument("--task-file")
    ap.add_argument("--allow", action="append", required=True)
    ap.add_argument("--test-cmd", required=True)
    ap.add_argument("--check-cmd")
    ap.add_argument("--ctx", type=int, default=24576)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--max-steps", type=int, default=12)
    ap.add_argument("--max-out", type=int, default=1600)
    ap.add_argument("--repeat-limit", type=int, default=3,
                    help="обрыв после N подряд идентичных вызовов; 0 — выключить")
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out")
    args = ap.parse_args()

    if bool(args.task) == bool(args.task_file):
        ap.error("нужен ровно один из --task / --task-file")
    task = args.task if args.task else open(args.task_file, encoding="utf-8").read()
    repo = Repo(args.repo, args.allow)

    if not os.path.isdir(os.path.join(repo.root, ".git")):
        ap.error(f"{repo.root} — не git-репозиторий")
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo.root,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        ap.error("рабочее дерево не чистое; цикл требует чистый клон")

    messages = [
        {"role": "system", "content": system_prompt(args.allow)},
        {"role": "user", "content": task.strip()},
    ]
    steps: list[dict] = []
    accepted = False
    nudge = ""
    last_nudge = ""
    last_sig: str | None = None
    repeat = 0
    stop_reason = ""
    started = time.perf_counter()
    last_prompt = 0

    for step in range(1, args.max_steps + 1):
        budget = args.ctx - 1024
        if estimate_tokens(messages) > budget:
            shrink(messages, budget)
        if estimate_tokens(messages) > budget:
            stop_reason = "история не помещается в контекст"
            nudge = stop_reason
            break

        num_predict = args.max_out
        if last_prompt:
            num_predict = max(256, min(num_predict, args.ctx - last_prompt - 512))

        if nudge:
            content = nudge
            if nudge == last_nudge:
                content = ("Результат и проверка не изменились с прошлого шага. "
                           "Не повторяй то же действие — смени подход.")
            messages.append({"role": "user", "content": content})
            last_nudge = nudge
            nudge = ""

        t0 = time.perf_counter()
        try:
            r = chat(messages, args.ctx, num_predict, args.think, args.model, args.host)
        except (urllib.error.URLError, TimeoutError) as e:
            nudge = f"Ошибка запроса к Ollama: {e}. Продолжай инструментами."
            steps.append({"step": step, "error": str(e)})
            continue

        msg = r.get("message") or {}
        calls = msg.get("tool_calls") or []
        parsed: list[tuple[str, dict]] = []
        for c in calls:
            fn = c["function"]
            a = fn.get("arguments")
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except json.JSONDecodeError:
                    a = {}
            parsed.append((fn["name"], a or {}))
        sig = calls_signature(parsed) if parsed else None
        if sig and sig == last_sig:
            repeat += 1
        elif sig:
            repeat = 1
        else:
            repeat = 0
        last_sig = sig

        last_prompt = int(r.get("prompt_eval_count") or last_prompt)
        entry = {
            "step": step,
            "secs": round(time.perf_counter() - t0, 1),
            "prompt_tok": r.get("prompt_eval_count"),
            "out_tok": r.get("eval_count"),
            "finish": r.get("done_reason"),
            "tools": [name for name, _ in parsed],
            "calls": [{"name": name, "args": brief_args(a)} for name, a in parsed],
            "repeat": repeat,
            "content": (msg.get("content") or "")[:400],
        }
        steps.append(entry)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

        if calls:
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": calls,
            })
            for name, a in parsed:
                result = tool_result(repo, name, a, args.test_cmd, args.timeout)
                messages.append({"role": "tool", "tool_name": name, "content": result})

        g = gate(repo, args.allow, args.test_cmd, args.check_cmd, args.timeout)
        entry["gate"] = {
            "tests_ok": g["tests_ok"], "changed": g["changed"],
            "violations": g["violations"], "check_ok": g["check_ok"],
        }
        if g["passed"]:
            accepted = True
            break

        if args.repeat_limit and repeat >= args.repeat_limit:
            stop_reason = f"нет прогресса: {repeat} одинаковых вызовов подряд"
            break

        if calls:
            if g["violations"]:
                nudge = ("Проверка: правки вне разрешённых путей: "
                         + ", ".join(g["violations"]) + ". Продолжай только в разрешённых файлах.")
            elif not g["tests_ok"]:
                nudge = ("Проверка: тесты не проходят (exit={}). Фактический вывод:\n{}"
                         .format(g["tests_exit"], g["tests_tail"][-2000:]))
            elif not g["diff_nonempty"]:
                nudge = "Проверка: тесты зелёные, но дифф пуст — задача не выполнена."
            elif g["check_ok"] is False:
                nudge = ("Проверка: внешняя приёмка не пройдена (exit={}). "
                         "Доведи решение, не подгоняя тесты.".format(g["check_exit"]))
        else:
            if g["violations"]:
                nudge = "Ты закончил, но есть правки вне разрешённых путей: " + ", ".join(g["violations"])
            elif not g["tests_ok"]:
                nudge = ("Ты объявил завершение, но тесты не проходят (exit={}). "
                         "Вот фактический вывод:\n{}").format(g["tests_exit"], g["tests_tail"][-2000:])
            elif not g["diff_nonempty"]:
                nudge = "Ты объявил завершение, но в репозитории нет изменений."
            elif g["check_ok"] is False:
                nudge = "Ты закончил, но внешняя приёмка не пройдена."
            else:
                accepted = True
                break

    final = gate(repo, args.allow, args.test_cmd, args.check_cmd, args.timeout)
    if final["passed"]:
        accepted = True
    if not stop_reason and not accepted:
        stop_reason = f"исчерпаны шаги ({args.max_steps})"
    total = time.perf_counter() - started
    claim = next((s.get("content") for s in reversed(steps) if s.get("content")), "")

    result = {
        "repo": repo.root,
        "allow": args.allow,
        "test_cmd": args.test_cmd,
        "check_cmd": args.check_cmd,
        "ctx": args.ctx,
        "host": args.host,
        "model": args.model,
        "think": args.think,
        "repeat_limit": args.repeat_limit,
        "stop_reason": stop_reason,
        "accepted": accepted,
        "steps": steps,
        "gate": {k: v for k, v in final.items() if k not in ("tests_tail", "check_tail")},
        "tests_tail": final["tests_tail"],
        "check_tail": final["check_tail"] if args.check_cmd else "",
        "diff_stat": repo.diff_stat(),
        "model_claim": claim,
        "seconds": round(total, 1),
    }
    out = args.out or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"verify-loop-{time.strftime('%Y%m%d-%H%M%S')}.json",
    )
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    check_label = "нет" if final["check_ok"] is None else ("ok" if final["check_ok"] else "fail")
    print(f"\nитог: {'ПРИНЯТО' if accepted else 'НЕ ПРИНЯТО'} · шагов {len(steps)} · "
          f"{total:.1f} s · тесты={'ok' if final['tests_ok'] else 'fail'} · "
          f"приёмка={check_label} · "
          f"дифф={len(final['changed'])} · нарушений={len(final['violations'])}")
    print("изменено:", ", ".join(final["changed"]) or "ничего")
    if stop_reason:
        print("остановка:", stop_reason)
    if claim:
        print("заявление модели (не влияет на приёмку):", claim.strip()[:300])
    print("артефакт:", out)
    return 0 if accepted else 1


if __name__ == "__main__":
    sys.exit(main())
