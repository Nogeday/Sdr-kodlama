"""Locate monorepo resource folders (config/, contracts/, cal/, reports/).

The code is meant to be used from a checkout (``pip install -e .``). Each folder can be
overridden with an environment variable for deployments:

    EDS_CONFIG_DIR, EDS_CONTRACTS_DIR, EDS_CAL_DIR, EDS_REPORTS_DIR
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def repo_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").is_file() and (p / "contracts").is_dir():
            return p
    return Path.cwd()


def _dir(env: str, default: str) -> Path:
    v = os.environ.get(env)
    return Path(v).expanduser().resolve() if v else repo_root() / default


def config_dir() -> Path:
    return _dir("EDS_CONFIG_DIR", "config")


def profiles_dir() -> Path:
    return config_dir() / "profiles"


def contracts_dir() -> Path:
    return _dir("EDS_CONTRACTS_DIR", "contracts")


def cal_dir() -> Path:
    return _dir("EDS_CAL_DIR", "cal")


def reports_dir() -> Path:
    return _dir("EDS_REPORTS_DIR", "reports")
