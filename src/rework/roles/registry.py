"""Role registry: auto-discovery of role modules in rework.roles.builtin.

Adding a role = dropping a module in builtin/ that subclasses RoleSpec. The
orchestrator reads name/description to choose roles; this registry maps names to
classes and instantiates on demand (specs are stateless; instances hold nothing).
"""

from __future__ import annotations

import importlib
import pkgutil

from rework.roles.base import RoleSpec


class RoleRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, type[RoleSpec]] = {}

    def register(self, spec_cls: type[RoleSpec]) -> None:
        if not spec_cls.name:
            raise ValueError(f"{spec_cls.__name__} must set a name")
        if spec_cls.name in self._specs:
            raise ValueError(f"duplicate role name: {spec_cls.name}")
        self._specs[spec_cls.name] = spec_cls

    def get(self, name: str) -> RoleSpec:
        try:
            return self._specs[name]()
        except KeyError:
            raise KeyError(f"unknown role {name!r}; known: {sorted(self._specs)}") from None

    def names(self) -> list[str]:
        return sorted(self._specs)

    def describe(self) -> dict[str, str]:
        return {name: self._specs[name].description for name in self.sorted_by_name()}

    def sorted_by_name(self) -> list[str]:
        return sorted(self._specs)


def autodiscover(package: str = "rework.roles.builtin") -> RoleRegistry:
    """Import every module in the builtin package; each registers nothing itself,
    we collect RoleSpec subclasses found in each module's namespace."""
    from rework.roles.base import RoleSpec as _Spec

    registry = RoleRegistry()
    module = importlib.import_module(package)
    for mod_info in pkgutil.iter_modules(module.__path__):
        mod = importlib.import_module(f"{package}.{mod_info.name}")
        for obj in vars(mod).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, _Spec)
                and obj is not _Spec
                and obj.__module__ == mod.__name__
                and not obj.__name__.startswith("_")
            ):
                registry.register(obj)
    return registry
