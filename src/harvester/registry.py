"""Finding sources: installed entry points, or a path to a Python file."""

from __future__ import annotations

import importlib.util
import inspect
import sys
from importlib.metadata import entry_points
from pathlib import Path

from harvester.source import Source

GROUP = "harvester.sources"


class SourceNotFound(LookupError):
    pass


def installed_sources() -> dict[str, type[Source]]:
    """Every source registered under the ``harvester.sources`` entry-point group.

    Third-party packages add sources by declaring, in their pyproject.toml::

        [project.entry-points."harvester.sources"]
        my-site = "my_package.sources:MySiteSource"
    """
    found: dict[str, type[Source]] = {}
    for ep in entry_points(group=GROUP):
        cls = ep.load()
        if isinstance(cls, type) and issubclass(cls, Source):
            found[cls.name or ep.name] = cls
    return dict(sorted(found.items()))


def load_source(spec: str) -> type[Source]:
    """Resolve ``name``, ``path/to/file.py`` or ``path/to/file.py:ClassName``."""
    # Split on the *last* colon, and only after ".py", so Windows drive
    # letters (C:\...) are not mistaken for the ":ClassName" suffix.
    if spec.endswith(".py"):
        return _from_file(Path(spec), None)
    if ".py:" in spec:
        path_part, _, class_name = spec.rpartition(":")
        return _from_file(Path(path_part), class_name)
    sources = installed_sources()
    if spec in sources:
        return sources[spec]
    raise SourceNotFound(f"unknown source {spec!r}; installed: {', '.join(sources) or 'none'}")


def _from_file(path: Path, class_name: str | None) -> type[Source]:
    if not path.is_file():
        raise SourceNotFound(f"no such file: {path}")
    module_name = f"harvester_user_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SourceNotFound(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if class_name:
        cls = getattr(module, class_name, None)
        if not (isinstance(cls, type) and issubclass(cls, Source)):
            raise SourceNotFound(f"{path} has no Source subclass named {class_name}")
        return cls

    defined = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Source) and obj.__module__ == module_name and obj.name
    ]
    if len(defined) != 1:
        names = ", ".join(c.__name__ for c in defined) or "none"
        raise SourceNotFound(
            f"{path} defines {len(defined)} sources ({names}); use {path}:ClassName"
        )
    return defined[0]
