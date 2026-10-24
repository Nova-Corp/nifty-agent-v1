"""Config loading with ${ENV_VAR} substitution (secrets live in .env, not in YAML)."""
from __future__ import annotations
import os
import re
from pathlib import Path
import yaml

_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
REQUIRED = {"mode", "broker", "underlying", "strategy", "risk", "execution", "charges"}


def _load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _sub(v):
    if isinstance(v, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _sub(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_sub(x) for x in v]
    return v


def load_config(path: str = "config.yaml") -> dict:
    _load_dotenv(Path(path).resolve().with_name(".env"))
    with open(path) as f:
        cfg = _sub(yaml.safe_load(f))
    missing = REQUIRED - set(cfg)
    if missing:
        raise ValueError(f"config missing sections: {sorted(missing)}")
    if cfg["mode"] not in ("paper", "live"):
        raise ValueError("mode must be 'paper' or 'live'")
    r = cfg["risk"]
    if not 0 < float(r["sl_pct"]) < 100:
        raise ValueError("risk.sl_pct must be between 0 and 100")
    if float(r["max_daily_loss"]) <= 0:
        raise ValueError("risk.max_daily_loss must be positive")
    if int(r["max_lots"]) < 1:
        raise ValueError("risk.max_lots must be >= 1")
    return cfg
