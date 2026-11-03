"""Start-up checks of the module registry."""

import pytest

from armadok.core.module import Module, Provider, Right
from armadok.core.registry import Registry, RegistryError


class _Dummy:
    pass


def test_valid_modules_are_accepted() -> None:
    registry = Registry(
        [
            Module(name="nodes"),
            Module(
                name="dns",
                requires=("nodes",),
                rights=(Right("dns.read"), Right("dns.write", depends_on=("dns.read",))),
                providers=(Provider("dns", "example", _Dummy, frozenset({"zone"})),),
            ),
        ]
    )
    assert set(registry.rights()) == {"dns.read", "dns.write"}
    assert set(registry.providers("dns")) == {"example"}


def test_duplicate_module_is_refused() -> None:
    with pytest.raises(RegistryError, match="duplicate module"):
        Registry([Module(name="a"), Module(name="a")])


def test_missing_required_module_is_refused() -> None:
    with pytest.raises(RegistryError, match="requires"):
        Registry([Module(name="dns", requires=("nodes",))])


def test_unknown_right_dependency_is_refused() -> None:
    with pytest.raises(RegistryError, match="unknown right"):
        Registry([Module(name="a", rights=(Right("a.write", depends_on=("a.read",)),))])


def test_duplicate_right_across_modules_is_refused() -> None:
    with pytest.raises(RegistryError, match="duplicate right"):
        Registry(
            [
                Module(name="a", rights=(Right("x"),)),
                Module(name="b", rights=(Right("x"),)),
            ]
        )


def test_duplicate_provider_is_refused() -> None:
    provider = Provider("dns", "p", _Dummy)
    with pytest.raises(RegistryError, match="duplicate provider"):
        Registry([Module(name="a", providers=(provider,)), Module(name="b", providers=(provider,))])
