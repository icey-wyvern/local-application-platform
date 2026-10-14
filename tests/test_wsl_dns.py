from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[1] / "tools/configure-wsl-lab-dns.py"
    spec = importlib.util.spec_from_file_location("configure_wsl_lab_dns", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wsl_dns_preserves_other_configuration() -> None:
    configure = _module().with_wsl_network_setting
    original = "[boot]\nsystemd=true\n\n[user]\ndefault=example-admin\n"
    expected = original.rstrip("\n") + "\n\n[network]\ngenerateResolvConf=false\n"
    assert configure(original) == expected
    assert configure(expected) == expected


def test_wsl_dns_replaces_existing_setting_and_rejects_ambiguity() -> None:
    configure = _module().with_wsl_network_setting
    original = "[network]\ngenerateResolvConf=true\n\n[user]\ndefault=example-admin\n"
    expected = original.replace("generateResolvConf=true", "generateResolvConf=false")
    assert configure(original) == expected
    with pytest.raises(ValueError, match="duplicate"):
        configure("[network]\ngenerateResolvConf=true\ngenerateResolvConf=false\n")
