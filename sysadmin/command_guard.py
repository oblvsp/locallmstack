"""
title: Command Guard
author: vsp
version: 1.0.0
license: MIT
description: Предупреждает в ответе, если модель показала команду высокого риска. Ничего не блокирует, не выполняет и не меняет команду.
"""

import re

MARKER = "Проверьте перед выполнением"
WARNING = (
    "⚠️ Проверьте перед выполнением: в ответе есть команда высокого риска "
    "({label}): `{fragment}`. Она не выполнялась. Сверьте путь и службу, "
    "сделайте резервную копию и запускайте только после явного подтверждения."
)

PATTERNS = [
    ("удаление корня или домашнего каталога", re.compile(r"\brm\s+-[A-Za-z]*[rf][A-Za-z]*\s+(?:/|~|\$HOME)(?:\s|$|\*)")),
    ("форматирование файловой системы", re.compile(r"\bmkfs(?:\.\w+)?\b")),
    ("запись образа на диск", re.compile(r"\bdd\b[^\n]*\bof=/dev/(?:sd|nvme|mmcblk|hd)")),
    ("перезапись устройства", re.compile(r">\s*/dev/(?:sd|nvme|mmcblk|hd)")),
    ("права 777 на корень", re.compile(r"\bchmod\s+(?:-R\s+)?777\s+/(?:\s|$)")),
    ("смена владельца корня", re.compile(r"\bchown\s+-R\s+\S+\s+/(?:\s|$)")),
    ("остановка или отключение службы", re.compile(r"\bsystemctl\s+(?:stop|disable|mask)\b")),
    ("удаление пакетов", re.compile(r"\b(?:apt|apt-get|dnf|pacman)\s+(?:remove|purge|--remove)\b")),
    ("выключение или перезагрузка", re.compile(r"\b(?:shutdown|poweroff|halt)\s+-|\bsystemctl\s+(?:reboot|poweroff)\b")),
    ("сброс сетевых правил", re.compile(r"\biptables\s+-F\b|\bufw\s+disable\b")),
    ("выполнение скачанного скрипта", re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z|da)?sh\b")),
    ("fork-бомба", re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:")),
    ("удаление docker-томов", re.compile(r"\bdocker\s+(?:volume\s+rm|system\s+prune)\b")),
]


class Filter:
    def outlet(self, body: dict, __user__: dict | None = None) -> dict:
        messages = body.get("messages") or []
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                return body
            if MARKER in content:
                return body
            for label, pattern in PATTERNS:
                match = pattern.search(content)
                if not match:
                    continue
                fragment = match.group(0).strip()[:80]
                warning = WARNING.format(label=label, fragment=fragment)
                message["content"] = warning + "\n\n" + content
                self._prepend_output(message, warning)
                break
            return body
        return body

    @staticmethod
    def _prepend_output(message: dict, warning: str) -> None:
        output = message.get("output")
        if not isinstance(output, list):
            return
        for item in output:
            if not isinstance(item, dict):
                continue
            blocks = item.get("content")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "output_text"
                    and isinstance(block.get("text"), str)
                ):
                    block["text"] = warning + "\n\n" + block["text"]
                    return
