"""
title: Host Metrics
author: vsp
version: 1.0.0
license: MIT
description: Только чтение — метрики хоста (CPU, RAM, диски, GPU, failed units) и состояние служб через локальный агент. Ничего не меняет.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("HOSTAGENT_URL", "http://host.docker.internal:8767").rstrip("/")
TIMEOUT = 12


def _get(path: str) -> dict:
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _gb(value) -> str:
    try:
        return f"{int(value) / 1024 ** 3:.1f} ГБ"
    except (TypeError, ValueError):
        return "?"


def _uptime(seconds) -> str:
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "?"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}д {hours}ч" if days else f"{hours}ч {rest // 60}м"


class Tools:
    def host_metrics(self) -> str:
        """Метрики хоста: uptime, load, RAM, swap, диски, GPU, failed units, топ процессов.

        Используй, когда нужно узнать состояние машины: загрузку CPU, свободную память,
        место на диске, температуру и память GPU, упавшие службы.
        """
        try:
            data = _get("/metrics")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return f"ошибка агента метрик ({BASE}): {exc}. Проверь: systemctl --user status hostagent"

        mem = data.get("memory") or {}
        lines = [
            f"Хост {data.get('hostname', '?')}: uptime {_uptime(data.get('uptime_seconds'))}, "
            f"CPU {data.get('cpu_count', '?')} ядер, load {', '.join(str(x) for x in data.get('load') or []) or '?'}",
            f"RAM: {_gb(mem.get('used'))} из {_gb(mem.get('total'))} ({mem.get('percent', '?')}%), "
            f"swap свободно {_gb(mem.get('swap_free'))} из {_gb(mem.get('swap_total'))}",
        ]
        for disk in data.get("disks") or []:
            lines.append(
                f"Диск {disk.get('target')}: {_gb(disk.get('used'))} из {_gb(disk.get('total'))} "
                f"({disk.get('percent')}%), свободно {_gb(disk.get('avail'))}"
            )
        gpu = data.get("gpu")
        if gpu:
            lines.append(
                f"GPU {gpu.get('name')}: VRAM {_gb(gpu.get('memory_used'))} из {_gb(gpu.get('memory_total'))}, "
                f"загрузка {gpu.get('util_percent')}%, {gpu.get('temperature_c')}°C"
            )
        failed = (data.get("failed_units") or []) + (data.get("user_failed_units") or [])
        lines.append("Упавшие службы: " + ("; ".join(failed) if failed else "нет"))
        top = data.get("top_processes") or []
        if top:
            lines.append(
                "Топ по CPU: "
                + "; ".join(f"{p.get('command')} {p.get('cpu_percent')}%" for p in top[:4])
            )
        return "\n".join(lines)[:1800]

    def host_unit(self, unit: str, lines: int = 5) -> str:
        """Состояние службы и последние записи журнала.

        Args:
            unit: имя юнита systemd, например bastion.service или ssh.service.
            lines: сколько последних строк журнала вернуть, 1–20.
        """
        try:
            lines = max(1, min(20, int(lines)))
        except (TypeError, ValueError):
            lines = 5
        try:
            state = _get("/unit?" + urllib.parse.urlencode({"name": unit}))
            log = _get("/journal?" + urllib.parse.urlencode({"unit": unit, "lines": lines}))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            return f"ошибка агента метрик ({BASE}): {exc}. Проверь: systemctl --user status hostagent"

        if state.get("error"):
            return f"{unit}: ошибка — {state['error']}"
        log_text = (log.get("text") or "").strip() or "(журнал пуст)"
        return (
            f"{unit} [{state.get('scope', '?')}]: {state.get('ActiveState', '?')}/"
            f"{state.get('SubState', '?')}, unit file: {state.get('UnitFileState', '?')}, "
            f"перезапусков: {state.get('NRestarts', '?')}\n"
            f"Последние записи журнала:\n{log_text[:1200]}"
        )
