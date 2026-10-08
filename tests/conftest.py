"""Import the flat plugin package under a Python-legal name.

The plugin ships as a **flat directory plugin** — ``plugin.yaml`` plus sibling
modules, exactly what ``~/.hermes/plugins/discord-presence/`` holds. A
hyphenated directory name is not a legal Python identifier, so the tests import
the same modules under the underscored alias rather than depending on the
on-disk spelling.

The mechanism mirrors how Hermes itself loads a directory plugin
(``hermes_cli/plugin_validate.py``'s capability probe uses the same
``spec_from_file_location`` + ``submodule_search_locations`` pair), so what the
tests exercise is the real import path, not a test-only shim.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "discord-presence"
PACKAGE = "discord_presence"

if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: ``__init__.py`` does ``from . import metrics``, which
    # resolves through this sys.modules entry — the classic package bootstrap
    # ordering. ``__path__`` is what makes the relative siblings findable.
    module.__path__ = [str(PLUGIN_DIR)]
    module.__package__ = PACKAGE
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)

    # A stray flat import (``import metrics``) would build a SECOND copy of the
    # module with its own globals, silently splitting the singleton state the
    # tests assert on. Pre-seed the aliases so both spellings share one object.
    for name in ("config", "metrics", "presence", "pulse_tool", "render", "schemas"):
        sys.modules.setdefault(name, importlib.import_module(f"{PACKAGE}.{name}"))
