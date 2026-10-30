#!/usr/bin/env python3
"""Замеры локального маршрута Ollama для задач агентной разработки.

Только чтение конфигурации и запросы к уже установленной модели.
Ничего не скачивает, не меняет systemd/конфиги. Модель выгружается
только для замера холодного старта (это штатная операция Ollama).

    python3 bench_local.py                 # тёплые прогоны + tool-call
    python3 bench_local.py --cold          # + холодный старт
    python3 bench_local.py --runs 3 --host http://127.0.0.1:11435

Пишет строки в results.tsv рядом со скриптом.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TSV = os.path.join(HERE, "results.tsv")

PROMPT = (
    "Перечисли три причины, по которым локальная модель может ошибиться "
    "при правке кода, и для каждой укажи проверку, которая это поймает. "
    "Кратко, без вступления."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Прочитать файл из рабочего каталога",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "путь к файлу"},
                    "start_line": {"type": "integer", "description": "с какой строки"},
                    "max_lines": {"type": "integer", "description": "сколько строк"},
                },
                "required": ["path"],
            },
        },
    }
]
TOOL_MESSAGES = [
    {
        "role": "system",
        "content": "Ты агент в терминале. Используй инструменты, когда нужен файл.",
    },
    {
        "role": "user",
        "content": "Прочитай первые 40 строк файла bench/bench_local.py и скажи, что он измеряет.",
    },
]


def http_json(url: str, payload: dict | None = None, timeout: int = 300) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def stream_lines(url: str, payload: dict, timeout: int = 300):
    """Генератор (t, line) для NDJSON (Ollama) и SSE (OpenAI-совместимый)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.strip()
            if not line:
                continue
            yield time.perf_counter() - t0, line.decode("utf-8", "replace")


def gpu():
    q = "memory.used,memory.total,temperature.gpu,power.draw,clocks.sm,utilization.gpu"
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        used, total, temp, power, sm, util = [x.strip() for x in out.split(",")]
        return {"vram_used_mb": int(float(used)), "vram_total_mb": int(float(total)),
                "temp_c": float(temp), "power_w": float(power),
                "sm_mhz": int(float(sm)), "util_pct": int(float(util))}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def ram():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) // 1024
    info["swap_used_mb"] = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
    return {"ram_available_mb": info["MemAvailable"], "ram_used_mb":
            info["MemTotal"] - info["MemAvailable"], "swap_used_mb": info["swap_used_mb"]}


def unload(host: str, model: str) -> None:
    try:
        http_json(f"{host}/api/generate", {"model": model, "keep_alive": 0}, timeout=60)
    except urllib.error.URLError:
        pass
    for _ in range(60):
        ps = http_json(f"{host}/api/ps", timeout=20)
        if not ps.get("models"):
            return
        time.sleep(1)


def run_stream(host: str, model: str, num_ctx: int, temperature: float) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": True,
        "think": False,
        "options": {"num_ctx": num_ctx, "temperature": temperature, "num_predict": 320},
    }
    first_any = first_tok = None
    chunks = 0
    final = {}
    for t, line in stream_lines(f"{host}/api/chat", payload):
        first_any = first_any if first_any is not None else t
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("error"):
            return {"error": obj["error"]}
        msg = obj.get("message") or {}
        if msg.get("content") or msg.get("thinking"):
            chunks += 1
            first_tok = first_tok if first_tok is not None else t
        if obj.get("done"):
            final = obj
    if not final:
        return {"error": "no done event"}
    eval_n = final.get("eval_count", 0)
    eval_ns = final.get("eval_duration", 0)
    return {
        "first_any_s": round(first_any or 0, 3),
        "ttft_s": round(first_tok or 0, 3),
        "total_s": round(final["total_duration"] / 1e9, 3),
        "load_s": round(final.get("load_duration", 0) / 1e9, 3),
        "prompt_tok": final.get("prompt_eval_count", 0),
        "out_tok": eval_n,
        "decode_tps": round(eval_n / (eval_ns / 1e9), 2) if eval_ns else None,
        "prompt_tps": round(final.get("prompt_eval_count", 0) /
                           (final.get("prompt_eval_duration", 1) / 1e9), 2)
        if final.get("prompt_eval_duration") else None,
        "chunks": chunks,
        "finish": final.get("done_reason"),
    }


def run_tool_native(host: str, model: str, num_ctx: int) -> dict:
    payload = {
        "model": model,
        "messages": TOOL_MESSAGES,
        "tools": TOOLS,
        "stream": True,
        "think": False,
        "options": {"num_ctx": num_ctx, "temperature": 0.1},
    }
    calls, text, done = [], [], {}
    for t, line in stream_lines(f"{host}/api/chat", payload):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") or {}
        if msg.get("content"):
            text.append(msg["content"])
        for c in msg.get("tool_calls") or []:
            calls.append(c.get("function", {}))
        if obj.get("done"):
            done = obj
            break
    fn = calls[0] if calls else None
    return {
        "path": "native /api/chat",
        "tool_calls": len(calls),
        "fn": fn.get("name") if fn else None,
        "args_ok": bool(fn and _valid_args(fn.get("arguments"))),
        "args": fn.get("arguments") if fn else None,
        "text_len": len("".join(text)),
        "out_tok": done.get("eval_count", 0),
        "finish": done.get("done_reason"),
    }


def _valid_args(a) -> bool:
    if isinstance(a, dict):
        return "path" in a
    if isinstance(a, str):
        try:
            return "path" in json.loads(a)
        except json.JSONDecodeError:
            return False
    return False


def run_tool_openai(host: str, model: str, num_ctx: int) -> dict:
    """OpenAI-совместимый /v1 — именно этот API использует pi.dev."""
    payload = {
        "model": model,
        "messages": TOOL_MESSAGES,
        "tools": TOOLS,
        "stream": True,
        "max_tokens": 256,
        "temperature": 0.1,
    }
    calls, first_any, sse, buffered = {}, None, 0, 0
    for t, line in stream_lines(f"{host}/v1/chat/completions", payload):
        first_any = first_any if first_any is not None else t
        if line.startswith("data:"):
            sse += 1
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                continue
        else:
            # bastion 0.6.0 отдаёт буферизованное JSON-тело вместо SSE
            buffered += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            for c in d.get("tool_calls") or []:
                idx = c.get("index", 0)
                slot = calls.setdefault(idx, {"name": "", "arguments": ""})
                fn = c.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if ch.get("message", {}).get("tool_calls"):
                for i, c in enumerate(ch["message"]["tool_calls"]):
                    fn = c.get("function") or {}
                    calls[i] = {"name": fn.get("name", ""),
                                "arguments": fn.get("arguments", "")}
    fn = calls.get(0)
    return {
        "path": "openai /v1",
        "tool_calls": len(calls),
        "fn": fn["name"] if fn else None,
        "args_ok": bool(fn and _valid_args(fn["arguments"])),
        "args": fn["arguments"] if fn else None,
        "sse_events": sse,
        "buffered_json": buffered,
        "first_byte_s": round(first_any or 0, 3),
    }


def tool_roundtrip(host: str, model: str, num_ctx: int) -> dict:
    """Второй шаг цикла: отдаём результат инструмента и ждём финальный ответ."""
    payload = {
        "model": model,
        "messages": TOOL_MESSAGES
        + [{
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": "read_file",
                    "arguments": {"path": "bench/bench_local.py", "max_lines": 40},
                }
            }],
        }, {
            "role": "tool",
            "tool_name": "read_file",
            "content": "#!/usr/bin/env python3\n# скрипт измеряет TTFT, decode tok/s и формат tool-call",
        }],
        "tools": TOOLS,
        "stream": False,
        "think": False,
        "options": {"num_ctx": num_ctx, "temperature": 0.1, "num_predict": 200},
    }
    try:
        r = http_json(f"{host}/api/chat", payload)
    except urllib.error.HTTPError as e:
        return {"path": "tool roundtrip", "ok": False, "http": e.code,
                "body": e.read().decode()[:200]}
    msg = r.get("message") or {}
    return {
        "path": "tool roundtrip",
        "ok": bool(msg.get("content")),
        "out_tok": r.get("eval_count", 0),
        "finish": r.get("done_reason"),
        "reply": (msg.get("content") or "")[:160].replace("\n", " "),
    }


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)[:200]
    return str(v)


def log(rows: list[dict]) -> None:
    new = not os.path.exists(TSV)
    keys = ["ts", "kind", "model", "num_ctx", "ctx", "vram_used_mb", "ram_used_mb",
            "value"]
    with open(TSV, "a") as f:
        if new:
            f.write("\t".join(keys) + "\n")
        for r in rows:
            f.write("\t".join(fmt(r.get(k)) for k in keys) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://127.0.0.1:11435")
    ap.add_argument("--model", default=os.environ.get("BENCH_MODEL", "qwen3.5:9b"))
    ap.add_argument("--num-ctx", type=int, default=24576)
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--cold", action="store_true")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    host, model = args.host.rstrip("/"), args.model
    out = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "label": args.label,
        "host": host,
        "model": model,
        "num_ctx": args.num_ctx,
        "temperature": args.temperature,
        "ollama": subprocess.run(["ollama", "--version"], capture_output=True,
                                 text=True).stdout.strip(),
        "show": http_json(f"{host}/api/show", {"model": model}),
        "gpu_before": gpu(),
        "ram_before": ram(),
    }
    out["show"] = {k: out["show"].get(k) for k in ("details", "capabilities",
                                                   "model_info")}
    mi = out["show"]["model_info"] or {}
    out["show"]["context_length"] = mi.get("qwen35.context_length")
    out["show"]["block_count"] = mi.get("qwen35.block_count")
    out["show"]["head_count_kv"] = mi.get("qwen35.attention.head_count_kv")
    out["show"]["embedding_length"] = mi.get("qwen35.embedding_length")

    print(f"модель {model} @ {host}, num_ctx={args.num_ctx}", flush=True)
    if args.cold:
        print("выгружаю модель для холодного старта…", flush=True)
        unload(host, model)
        time.sleep(2)
        out["cold"] = run_stream(host, model, args.num_ctx, args.temperature)
        out["gpu_after_cold"] = gpu()
        print("  cold:", json.dumps(out["cold"], ensure_ascii=False), flush=True)

    out["warm"] = []
    for i in range(args.runs):
        r = run_stream(host, model, args.num_ctx, args.temperature)
        out["warm"].append(r)
        out["gpu_during"] = gpu()
        print(f"  warm#{i + 1}:", json.dumps(r, ensure_ascii=False), flush=True)

    print("tool-call, нативный /api/chat…", flush=True)
    out["tool_native"] = run_tool_native(host, model, args.num_ctx)
    print(" ", json.dumps(out["tool_native"], ensure_ascii=False), flush=True)

    print("tool-call, OpenAI-совместимый /v1…", flush=True)
    out["tool_openai"] = run_tool_openai(host, model, args.num_ctx)
    print(" ", json.dumps(out["tool_openai"], ensure_ascii=False), flush=True)

    print("второй шаг цикла (результат инструмента → ответ)…", flush=True)
    out["roundtrip"] = tool_roundtrip(host, model, args.num_ctx)
    print(" ", json.dumps(out["roundtrip"], ensure_ascii=False), flush=True)

    out["gpu_after"] = gpu()
    out["ram_after"] = ram()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(HERE, f"run-{stamp}.json")
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    rows = []
    if "cold" in out:
        rows.append({"ts": out["ts"], "kind": "cold", "model": model,
                     "num_ctx": args.num_ctx, "ctx": args.label,
                     "vram_used_mb": out["gpu_after_cold"].get("vram_used_mb"),
                     "ram_used_mb": out["ram_before"].get("ram_used_mb"),
                     "value": json.dumps(out["cold"], ensure_ascii=False)})
    for i, r in enumerate(out["warm"], 1):
        rows.append({"ts": out["ts"], "kind": f"warm{i}", "model": model,
                     "num_ctx": args.num_ctx, "ctx": args.label,
                     "vram_used_mb": out["gpu_during"].get("vram_used_mb"),
                     "ram_used_mb": ram().get("ram_used_mb"),
                     "value": json.dumps(r, ensure_ascii=False)})
    for k in ("tool_native", "tool_openai", "roundtrip"):
        rows.append({"ts": out["ts"], "kind": k, "model": model,
                     "num_ctx": args.num_ctx, "ctx": args.label,
                     "vram_used_mb": out["gpu_after"].get("vram_used_mb"),
                     "ram_used_mb": out["ram_after"].get("ram_used_mb"),
                     "value": json.dumps(out[k], ensure_ascii=False)})
    log(rows)
    print(f"\nсохранено: {path}\nTSV: {TSV}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
