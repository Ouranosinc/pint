"""
    pint.systems
    ~~~~~~~~~~~~

    Functions and classes related to system definitions and conversions.

    :copyright: 2016 by Pint Authors, see AUTHORS for more details.
    :license: BSD, see LICENSE for more details.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

from .babel_names import _babel_systems
from .compat import babel_parse
from .util import (
    SharedRegistryObject,
    SourceIterator,
    getattr_maybe_raise,
    logger,
    to_units_container,
)


@dataclass(frozen=True)
class SystemDefinition:
    """Definition of a System:

        @system <name> [using <group 1>, ..., <group N>]
            <rule 1>
            ...
            <rule N>
        @end

    The syntax for the rule is:

        new_unit_name : old_unit_name

    where:
        - old_unit_name: a root unit part which is going to be removed from the system.
        - new_unit_name: a non root unit which is going to replace the old_unit.

    If the new_unit_name and the old_unit_name, the later and the colon can be omitted.
    """

    #: Regex to match the header parts of a context.
    _header_re = re.compile(r"@system\s+(?P<name>\w+)\s*(using\s(?P<used_groups>.*))*")

    name: str
    unit_replacements: Tuple[Tuple[int, str, str], ...]
    using_group_names: Tuple[str, ...]

    @classmethod
    def from_lines(cls, lines, non_int_type=float):
        lines = SourceIterator(lines)

        lineno, header = next(lines)

        r = cls._header_re.search(header)

        if r is None:
            raise ValueError("Invalid System header syntax '%s'" % header)

        name = r.groupdict()["name"].strip()
        groups = r.groupdict()["used_groups"]

        # If the systems has no group, it automatically uses the root group.
        if groups:
            group_names = tuple(a.strip() for a in groups.split(","))
        else:
            group_names = ("root",)

        unit_replacements = []
        for lineno, line in lines:
            line = line.strip()

            # We would identify a
            #  - old_unit: a root unit part which is going to be removed from the system.
            #  - new_unit: a non root unit which is going to replace the old_unit.

            if ":" in line:
                # The syntax is new_unit:old_unit

                new_unit, old_unit = line.split(":")
                new_unit, old_unit = new_unit.strip(), old_unit.strip()

                unit_replacements.append((lineno, new_unit, old_unit))
            else:
                # The syntax is new_unit
                # old_unit is inferred as the root unit with the same dimensionality.
                unit_replacements.append((lineno, line, None))

        return cls(name, tuple(unit_replacements), group_names)


class System(SharedRegistryObject):
    """A system is a Group plus a set of base units.

    Members are computed dynamically, that is if a unit is added to a group X
    all groups that include X are affected.

    The System belongs to one Registry.

    See SystemDefinition for the definition file syntax.
    """

    def __init__(self, name):
        """
        :param name: Name of the group
        :type name: str
        """

        #: Name of the system
        #: :type: str
        self.name = name

        #: Maps root unit names to a dict indicating the new unit and its exponent.
        #: :type: dict[str, dict[str, number]]]
        self.base_units = {}

        #: Derived unit names.
        #: :type: set(str)
        self.derived_units = set()

        #: Names of the _used_groups in used by this system.
        #: :type: set(str)
        self._used_groups = set()

        #: :type: frozenset | None
        self._computed_members = None

        # Add this system to the system dictionary
        self._REGISTRY._systems[self.name] = self

    def __dir__(self):
        return list(self.members)

    def __getattr__(self, item):
        getattr_maybe_raise(self, item)
        u = getattr(self._REGISTRY, self.name + "_" + item, None)
        if u is not None:
            return u
        return getattr(self._REGISTRY, item)

    @property
    def members(self):
        d = self._REGISTRY._groups
        if self._computed_members is None:
            self._computed_members = set()

            for group_name in self._used_groups:
                try:
                    self._computed_members |= d[group_name].members
                except KeyError:
                    logger.warning(
                        "Could not resolve {} in System {}".format(
                            group_name, self.name
                        )
                    )

            self._computed_members = frozenset(self._computed_members)

        return self._computed_members

    def invalidate_members(self):
        """Invalidate computed members in this Group and all parent nodes."""
        self._computed_members = None

    def add_groups(self, *group_names):
        """Add groups to group."""
        self._used_groups |= set(group_names)

        self.invalidate_members()

    def remove_groups(self, *group_names):
        """Remove groups from group."""
        self._used_groups -= set(group_names)

        self.invalidate_members()

    def format_babel(self, locale):
        """translate the name of the system."""
        if locale and self.name in _babel_systems:
            name = _babel_systems[self.name]
            locale = babel_parse(locale)
            return locale.measurement_systems[name]
        return self.name

    @classmethod
    def from_lines(cls, lines, get_root_func, non_int_type=float):
        system_definition = SystemDefinition.from_lines(lines, get_root_func)
        return cls.from_definition(system_definition, get_root_func)

    @classmethod
    def from_definition(cls, system_definition: SystemDefinition, get_root_func):
        base_unit_names = {}
        derived_unit_names = []
        for lineno, new_unit, old_unit in system_definition.unit_replacements:
            if old_unit is None:
                old_unit_dict = to_units_container(get_root_func(new_unit)[1])

                if len(old_unit_dict) != 1:
                    raise ValueError(
                        "The new base must be a root dimension if not discarded unit is specified."
                    )

                old_unit, value = dict(old_unit_dict).popitem()

                base_unit_names[old_unit] = {new_unit: 1 / value}
            else:
                # The old unit MUST be a root unit, if not raise an error.
                if old_unit != str(get_root_func(old_unit)[1]):
                    raise ValueError(
                        "In `%s`, the unit at the right of the `:` (%s) must be a root unit."
                        % (lineno, old_unit)
                    )

                # Here we find new_unit expanded in terms of root_units
                new_unit_expanded = to_units_container(
                    get_root_func(new_unit)[1], cls._REGISTRY
                )

                # We require that the old unit is present in the new_unit expanded
                if old_unit not in new_unit_expanded:
                    raise ValueError("Old unit must be a component of new unit")

                # Here we invert the equation, in other words
                # we write old units in terms new unit and expansion
                new_unit_dict = {
                    new_unit: -1 / value
                    for new_unit, value in new_unit_expanded.items()
                    if new_unit != old_unit
                }
                new_unit_dict[new_unit] = 1 / new_unit_expanded[old_unit]

                base_unit_names[old_unit] = new_unit_dict

        system = cls(system_definition.name)
        system.add_groups(*system_definition.using_group_names)
        system.base_units.update(**base_unit_names)
        system.derived_units |= set(derived_unit_names)

        return system


class Lister:
    def __init__(self, d):
        self.d = d

    def __dir__(self):
        return list(self.d.keys())

    def __getattr__(self, item):
        getattr_maybe_raise(self, item)
        return self.d[item]


_System = System


def build_system_class(registry):
    class System(_System):
        _REGISTRY = registry

    return System
