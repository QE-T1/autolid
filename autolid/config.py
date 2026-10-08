from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"


class Section(dict):
    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as e:
            raise AttributeError(item) from e


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return Section({k: _wrap(v) for k, v in value.items()})
    return value


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def ensure_config() -> None:
    if not CONFIG_PATH.exists():
        shutil.copy(EXAMPLE_PATH, CONFIG_PATH)


def load_config() -> Section:
    ensure_config()
    defaults = yaml.safe_load(EXAMPLE_PATH.read_text(encoding="utf-8")) or {}
    user = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8-sig")) or {}
    return _wrap(_merge(defaults, user))


def path_of(cfg: Section, key: str) -> Path:
    p = Path(cfg.paths[key])
    return p if p.is_absolute() else ROOT / p


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_config_value(section: str, key: str, value: Any) -> None:
    ensure_config()
    lines = CONFIG_PATH.read_text(encoding="utf-8-sig").splitlines()
    new_line = None
    in_section = False
    section_end = len(lines)
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}:\s*(#.*)?$", line):
            in_section = True
            section_end = len(lines)
            continue
        if in_section:
            if re.match(r"^\S", line) and not line.startswith("#"):
                section_end = i
                break
            m = re.match(rf"^(\s+){re.escape(key)}:(\s*)(.*?)(\s+#.*)?$", line)
            if m:
                lines[i] = f"{m.group(1)}{key}: {_yaml_scalar(value)}{m.group(4) or ''}"
                new_line = i
                break
    if new_line is None:
        if in_section:
            lines.insert(section_end, f"  {key}: {_yaml_scalar(value)}")
        else:
            lines += ["", f"{section}:", f"  {key}: {_yaml_scalar(value)}"]
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
