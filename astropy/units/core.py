# Licensed under a 3-clause BSD style license - see LICENSE.rst

"""
Core units classes and functions.
"""

from __future__ import annotations

import inspect
import operator
import textwrap
import unicodedata
import warnings
from collections.abc import Collection, Iterable, Mapping, MutableMapping, Sequence
from functools import cached_property
from threading import RLock
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Literal, NamedTuple, Self, overload

import numpy as np

from astropy.utils.compat import COPY_IF_NEEDED
from astropy.utils.decorators import deprecated, lazyproperty
from astropy.utils.exceptions import AstropyDeprecationWarning, AstropyWarning

from .errors import UnitConversionError, UnitParserWarning, UnitsError, UnitsWarning
from .utils import (
    is_effectively_unity,
    resolve_fractions,
    sanitize_power,
    sanitize_scale,
)

if TYPE_CHECKING:
    from .format import Base
    from .physical import PhysicalType
    from .quantity import Quantity
    from .typing import (
        PhysicalTypeID,
        UnitLike,
        UnitPower,
        UnitPowerLike,
        UnitScale,
        UnitScaleLike,
    )

__all__ = [
    "CompositeUnit",
    "IrreducibleUnit",
    "NamedUnit",
    "PrefixUnit",
    "Unit",
    "UnitBase",
    "UnitPrefix",
    "UnrecognizedUnit",
    "add_enabled_aliases",
    "add_enabled_equivalencies",
    "add_enabled_units",
    "def_unit",
    "dimensionless_unscaled",
    "get_current_unit_registry",
    "one",
    "set_enabled_aliases",
    "set_enabled_equivalencies",
    "set_enabled_units",
]

UNITY: Final[float] = 1.0

_WARNING_LOCK: Final[RLock] = RLock()
_WARNING_ACTIONS: Final[dict[str, str]] = {
    "silent": "ignore",
    "warn": "default",
    "raise": "error",
}


def _flatten_units_collection(items: object) -> set[UnitBase]:
    """
    Given a list of sequences, modules or dictionaries of units, or
    single units, return a flat set of all the units found.
    """
    if not isinstance(items, list):
        items = [items]

    result = set()
    for item in items:
        if isinstance(item, UnitBase):
            result.add(item)
        else:
            if isinstance(item, dict):
                units = item.values()
            elif inspect.ismodule(item):
                units = vars(item).values()
            elif np.iterable(item):
                units = item
            else:
                continue

            for unit in units:
                if isinstance(unit, UnitBase):
                    result.add(unit)

    return result


def _normalize_equivalencies(equivalencies):
    """Normalizes equivalencies ensuring each is a 4-tuple.

    The resulting tuple is of the form::

        (from_unit, to_unit, forward_func, backward_func)

    Parameters
    ----------
    equivalencies : list of equivalency pairs

    Raises
    ------
    ValueError if an equivalency cannot be interpreted
    """
    if equivalencies is None:
        return []

    normalized = []

    for i, equiv in enumerate(equivalencies):
        if len(equiv) == 2:
            funit, tunit = equiv
            a = b = lambda x: x
        elif len(equiv) == 3:
            funit, tunit, a = equiv
            b = a
        elif len(equiv) == 4:
            funit, tunit, a, b = equiv
        else:
            raise ValueError(f"Invalid equivalence entry {i}: {equiv!r}")
        if not (
            funit is Unit(funit)
            and (tunit is None or tunit is Unit(tunit))
            and callable(a)
            and callable(b)
        ):
            raise ValueError(f"Invalid equivalence entry {i}: {equiv!r}")
        normalized.append((funit, tunit, a, b))

    return normalized


class _UnitRegistry:
    """
    Manages a registry of the enabled units.
    """

    def __init__(self, init=[], equivalencies=[], aliases={}):
        if isinstance(init, _UnitRegistry):
            # If passed another registry we don't need to rebuild everything.
            # but because these are mutable types we don't want to create
            # conflicts so everything needs to be copied.
            self._equivalencies = init._equivalencies.copy()
            self._aliases = init._aliases.copy()
            self._all_units = init._all_units.copy()
            self._registry = init._registry.copy()
            self._non_prefix_units = init._non_prefix_units.copy()
            # The physical type is a dictionary containing sets as values.
            # All of these must be copied otherwise we could alter the old
            # registry.
            self._by_physical_type = {
                k: v.copy() for k, v in init._by_physical_type.items()
            }

        else:
            self._reset_units()
            self._reset_equivalencies()
            self._reset_aliases()
            self.add_enabled_units(init)
            self.add_enabled_equivalencies(equivalencies)
            self.add_enabled_aliases(aliases)

    def _reset_units(self) -> None:
        self._all_units = set()
        self._non_prefix_units = set()
        self._registry = {}
        self._by_physical_type = {}

    def _reset_equivalencies(self) -> None:
        self._equivalencies = set()

    def _reset_aliases(self) -> None:
        self._aliases = {}

    @property
    def registry(self) -> dict[str, UnitBase]:
        return self._registry

    @property
    def all_units(self) -> set[UnitBase]:
        return self._all_units

    @property
    def non_prefix_units(self) -> set[UnitBase]:
        return self._non_prefix_units

    def set_enabled_units(self, units: object) -> None:
        """
        Sets the units enabled in the unit registry.

        These units are searched when using
        `UnitBase.find_equivalent_units`, for example.

        Parameters
        ----------
        units : list of sequence, dict, or module
            This is a list of things in which units may be found
            (sequences, dicts or modules), or units themselves.  The
            entire set will be "enabled" for searching through by
            methods like `UnitBase.find_equivalent_units` and
            `UnitBase.compose`.
        """
        self._reset_units()
        return self.add_enabled_units(units)

    def add_enabled_units(self, units: object) -> None:
        """
        Adds to the set of units enabled in the unit registry.

        These units are searched when using
        `UnitBase.find_equivalent_units`, for example.

        Parameters
        ----------
        units : list of sequence, dict, or module
            This is a list of things in which units may be found
            (sequences, dicts or modules), or units themselves.  The
            entire set will be added to the "enabled" set for
            searching through by methods like
            `UnitBase.find_equivalent_units` and `UnitBase.compose`.
        """
        units = _flatten_units_collection(units)

        for unit in units:
            # Loop through all of the names first, to ensure all of them
            # are new, then add them all as a single "transaction" below.
            for st in unit._names:
                if st in self._registry and unit != self._registry[st]:
                    raise ValueError(
                        f"Object with name {st!r} already exists in namespace. "
                        "Filter the set of units to avoid name clashes before "
                        "enabling them."
                    )

            for st in unit._names:
                self._registry[st] = unit

            self._all_units.add(unit)
            if not isinstance(unit, PrefixUnit):
                self._non_prefix_units.add(unit)

            self._by_physical_type.setdefault(unit._physical_type_id, set()).add(unit)

    def get_units_with_physical_type(self, unit: UnitBase) -> set[UnitBase]:
        """
        Get all units in the registry with the same physical type as
        the given unit.

        Parameters
        ----------
        unit : UnitBase instance
        """
        return self._by_physical_type.get(unit._physical_type_id, set())

    @property
    def equivalencies(self):
        return list(self._equivalencies)

    def set_enabled_equivalencies(self, equivalencies):
        """
        Sets the equivalencies enabled in the unit registry.

        These equivalencies are used if no explicit equivalencies are given,
        both in unit conversion and in finding equivalent units.

        This is meant in particular for allowing angles to be dimensionless.
        Use with care.

        Parameters
        ----------
        equivalencies : list of tuple
            List of equivalent pairs, e.g., as returned by
            `~astropy.units.dimensionless_angles`.
        """
        self._reset_equivalencies()
        return self.add_enabled_equivalencies(equivalencies)

    def add_enabled_equivalencies(self, equivalencies):
        """
        Adds to the set of equivalencies enabled in the unit registry.

        These equivalencies are used if no explicit equivalencies are given,
        both in unit conversion and in finding equivalent units.

        This is meant in particular for allowing angles to be dimensionless.
        Use with care.

        Parameters
        ----------
        equivalencies : list of tuple
            List of equivalent pairs, e.g., as returned by
            `~astropy.units.dimensionless_angles`.
        """
        # pre-normalize list to help catch mistakes
        equivalencies = _normalize_equivalencies(equivalencies)
        self._equivalencies |= set(equivalencies)

    @property
    def aliases(self) -> dict[str, UnitBase]:
        return self._aliases

    def set_enabled_aliases(self, aliases: dict[str, UnitBase]) -> None:
        """
        Set aliases for units.

        Parameters
        ----------
        aliases : dict of str, Unit
            The aliases to set. The keys must be the string aliases, and values
            must be the `astropy.units.Unit` that the alias will be mapped to.

        Raises
        ------
        ValueError
            If the alias already defines a different unit.

        """
        self._reset_aliases()
        self.add_enabled_aliases(aliases)

    def add_enabled_aliases(self, aliases: dict[str, UnitBase]) -> None:
        """
        Add aliases for units.

        Parameters
        ----------
        aliases : dict of str, Unit
            The aliases to add. The keys must be the string aliases, and values
            must be the `astropy.units.Unit` that the alias will be mapped to.

        Raises
        ------
        ValueError
            If the alias already defines a different unit.

        """
        for alias, unit in aliases.items():
            if alias in self._registry and unit != self._registry[alias]:
                raise ValueError(
                    f"{alias} already means {self._registry[alias]}, so "
                    f"cannot be used as an alias for {unit}."
                )
            if alias in self._aliases and unit != self._aliases[alias]:
                raise ValueError(
                    f"{alias} already is an alias for {self._aliases[alias]}, so "
                    f"cannot be used as an alias for {unit}."
                )

        for alias, unit in aliases.items():
            if alias not in self._registry and alias not in self._aliases:
                self._aliases[alias] = unit


class _UnitContext:
    def __init__(self, init=[], equivalencies=[]):
        _unit_registries.append(_UnitRegistry(init=init, equivalencies=equivalencies))

    def __enter__(self) -> None:
        pass

    def __exit__(
        self,
        type: type[BaseException] | None,
        value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _unit_registries.pop()


_unit_registries: Final = [_UnitRegistry()]


def get_current_unit_registry() -> _UnitRegistry:
    return _unit_registries[-1]


def set_enabled_units(units: object) -> _UnitContext:
    """
    Sets the units enabled in the unit registry.

    These units are searched when using
    `UnitBase.find_equivalent_units`, for example.

    This may be used either permanently, or as a context manager using
    the ``with`` statement (see example below).

    Parameters
    ----------
    units : list of sequence, dict, or module
        This is a list of things in which units may be found
        (sequences, dicts or modules), or units themselves.  The
        entire set will be "enabled" for searching through by methods
        like `UnitBase.find_equivalent_units` and `UnitBase.compose`.

    Examples
    --------
    >>> from astropy import units as u
    >>> with u.set_enabled_units([u.pc]):
    ...     u.m.find_equivalent_units()
    ...
      Primary name | Unit definition | Aliases
    [
      pc           | 3.08568e+16 m   | parsec  ,
    ]
    >>> u.m.find_equivalent_units()
      Primary name | Unit definition | Aliases
    [
      AU           | 1.49598e+11 m   | au, astronomical_unit            ,
      Angstrom     | 1e-10 m         | AA, angstrom, Å                  ,
      cm           | 0.01 m          | centimeter                       ,
      earthRad     | 6.3781e+06 m    | R_earth, Rearth                  ,
      jupiterRad   | 7.1492e+07 m    | R_jup, Rjup, R_jupiter, Rjupiter ,
      lsec         | 2.99792e+08 m   | lightsecond                      ,
      lyr          | 9.46073e+15 m   | lightyear                        ,
      m            | irreducible     | meter                            ,
      micron       | 1e-06 m         |                                  ,
      pc           | 3.08568e+16 m   | parsec                           ,
      solRad       | 6.957e+08 m     | R_sun, Rsun                      ,
    ]
    """
    # get a context with a new registry, using equivalencies of the current one
    context = _UnitContext(equivalencies=get_current_unit_registry().equivalencies)
    # in this new current registry, enable the units requested
    get_current_unit_registry().set_enabled_units(units)
    return context


def add_enabled_units(units: object) -> _UnitContext:
    """
    Adds to the set of units enabled in the unit registry.

    These units are searched when using
    `UnitBase.find_equivalent_units`, for example.

    This may be used either permanently, or as a context manager using
    the ``with`` statement (see example below).

    Parameters
    ----------
    units : list of sequence, dict, or module
        This is a list of things in which units may be found
        (sequences, dicts or modules), or units themselves.  The
        entire set will be added to the "enabled" set for searching
        through by methods like `UnitBase.find_equivalent_units` and
        `UnitBase.compose`.

    Examples
    --------
    >>> from astropy import units as u
    >>> from astropy.units import imperial
    >>> with u.add_enabled_units(imperial):
    ...     u.m.find_equivalent_units()
    ...
      Primary name | Unit definition | Aliases
    [
      AU           | 1.49598e+11 m   | au, astronomical_unit            ,
      Angstrom     | 1e-10 m         | AA, angstrom, Å                  ,
      cm           | 0.01 m          | centimeter                       ,
      earthRad     | 6.3781e+06 m    | R_earth, Rearth                  ,
      ft           | 0.3048 m        | foot                             ,
      fur          | 201.168 m       | furlong                          ,
      inch         | 0.0254 m        |                                  ,
      jupiterRad   | 7.1492e+07 m    | R_jup, Rjup, R_jupiter, Rjupiter ,
      lsec         | 2.99792e+08 m   | lightsecond                      ,
      lyr          | 9.46073e+15 m   | lightyear                        ,
      m            | irreducible     | meter                            ,
      mi           | 1609.34 m       | mile                             ,
      micron       | 1e-06 m         |                                  ,
      mil          | 2.54e-05 m      | thou                             ,
      nmi          | 1852 m          | nauticalmile, NM                 ,
      pc           | 3.08568e+16 m   | parsec                           ,
      solRad       | 6.957e+08 m     | R_sun, Rsun                      ,
      yd           | 0.9144 m        | yard                             ,
    ]
    """
    # get a context with a new registry, which is a copy of the current one
    context = _UnitContext(get_current_unit_registry())
    # in this new current registry, enable the further units requested
    get_current_unit_registry().add_enabled_units(units)
    return context


def set_enabled_equivalencies(equivalencies):
    """
    Sets the equivalencies enabled in the unit registry.

    These equivalencies are used if no explicit equivalencies are given,
    both in unit conversion and in finding equivalent units.

    This is meant in particular for allowing angles to be dimensionless.
    Use with care.

    Parameters
    ----------
    equivalencies : list of tuple
        list of equivalent pairs, e.g., as returned by
        `~astropy.units.dimensionless_angles`.

    Examples
    --------
    Exponentiation normally requires dimensionless quantities.  To avoid
    problems with complex phases::

        >>> from astropy import units as u
        >>> with u.set_enabled_equivalencies(u.dimensionless_angles()):
        ...     phase = 0.5 * u.cycle
        ...     np.exp(1j*phase)  # doctest: +FLOAT_CMP
        <Quantity -1.+1.2246468e-16j>
    """
    # get a context with a new registry, using all units of the current one
    context = _UnitContext(get_current_unit_registry())
    # in this new current registry, enable the equivalencies requested
    get_current_unit_registry().set_enabled_equivalencies(equivalencies)
    return context


def add_enabled_equivalencies(equivalencies):
    """
    Adds to the equivalencies enabled in the unit registry.

    These equivalencies are used if no explicit equivalencies are given,
    both in unit conversion and in finding equivalent units.

    This is meant in particular for allowing angles to be dimensionless.
    Since no equivalencies are enabled by default, generally it is recommended
    to use `set_enabled_equivalencies`.

    Parameters
    ----------
    equivalencies : list of tuple
        list of equivalent pairs, e.g., as returned by
        `~astropy.units.dimensionless_angles`.
    """
    # get a context with a new registry, which is a copy of the current one
    context = _UnitContext(get_current_unit_registry())
    # in this new current registry, enable the further equivalencies requested
    get_current_unit_registry().add_enabled_equivalencies(equivalencies)
    return context


def set_enabled_aliases(aliases: dict[str, UnitBase]) -> _UnitContext:
    """
    Set aliases for units.

    This is useful for handling alternate spellings for units, or
    misspelled units in files one is trying to read.

    Parameters
    ----------
    aliases : dict of str, Unit
        The aliases to set. The keys must be the string aliases, and values
        must be the `astropy.units.Unit` that the alias will be mapped to.

    Raises
    ------
    ValueError
        If the alias already defines a different unit.

    Examples
    --------
    To temporarily allow for a misspelled 'Angstroem' unit::

        >>> from astropy import units as u
        >>> with u.set_enabled_aliases({'Angstroem': u.Angstrom}):
        ...     print(u.Unit("Angstroem", parse_strict="raise") == u.Angstrom)
        True

    """
    # get a context with a new registry, which is a copy of the current one
    context = _UnitContext(get_current_unit_registry())
    # in this new current registry, enable the further equivalencies requested
    get_current_unit_registry().set_enabled_aliases(aliases)
    return context


def add_enabled_aliases(aliases: dict[str, UnitBase]) -> _UnitContext:
    """
    Add aliases for units.

    This is useful for handling alternate spellings for units, or
    misspelled units in files one is trying to read.

    Since no aliases are enabled by default, generally it is recommended
    to use `set_enabled_aliases`.

    Parameters
    ----------
    aliases : dict of str, Unit
        The aliases to add. The keys must be the string aliases, and values
        must be the `astropy.units.Unit` that the alias will be mapped to.

    Raises
    ------
    ValueError
        If the alias already defines a different unit.

    Examples
    --------
    To temporarily allow for a misspelled 'Angstroem' unit::

        >>> from astropy import units as u
        >>> with u.add_enabled_aliases({'Angstroem': u.Angstrom}):
        ...     print(u.Unit("Angstroem", parse_strict="raise") == u.Angstrom)
        True

    """
    # get a context with a new registry, which is a copy of the current one
    context = _UnitContext(get_current_unit_registry())
    # in this new current registry, enable the further equivalencies requested
    get_current_unit_registry().add_enabled_aliases(aliases)
    return context


class UnitBase:
    """
    Abstract base class for units.

    Most of the arithmetic operations on units are defined in this
    base class.

    Should not be instantiated by users directly.
    """

    # Make sure that __rmul__ of units gets called over the __mul__ of Numpy
    # arrays to avoid element-wise multiplication.
    __array_priority__: Final = 1000

    def __deepcopy__(self, memo: dict[int, Any] | None) -> Self:
        # This may look odd, but the units conversion will be very
        # broken after deep-copying if we don't guarantee that a given
        # physical unit corresponds to only one instance
        return self

    def _repr_latex_(self) -> str:
        """
        Generate latex representation of unit name.  This is used by
        the IPython notebook to print a unit with a nice layout.

        Returns
        -------
        Latex string
        """
        from .format import Latex

        return Latex.to_string(self)

    def __bytes__(self) -> bytes:
        return str(self).encode("unicode_escape")

    def __str__(self) -> str:
        from .format import Generic

        return Generic.to_string(self)

    def __repr__(self) -> str:
        return f'Unit("{self}")'

    @cached_property
    def _physical_type_id(self) -> PhysicalTypeID:
        """
        Returns an identifier that uniquely identifies the physical
        type of this unit.  It is comprised of the bases and powers of
        this unit, without the scale.  Since it is hashable, it is
        useful as a dictionary key.
        """
        unit = self.decompose()
        return tuple(zip((base.name for base in unit.bases), unit.powers))

    @property
    def scale(self) -> UnitScale:
        """The scale of the unit."""
        return 1.0

    @property
    def bases(self) -> list[NamedUnit]:
        """The bases of the unit."""
        return [self]

    @property
    def powers(self) -> list[UnitPower]:
        """The powers of the bases of the unit."""
        return [1]

    def to_string(self, format: type[Base] | str | None = None, **kwargs) -> str:
        r"""Output the unit in the given format as a string.

        Parameters
        ----------
        format : `astropy.units.format.Base` subclass or str or None
            The name of a format or a formatter class.  If not
            provided (or `None`), defaults to the generic format.

        **kwargs
            Further options forwarded to the formatter. Currently
            recognized is ``fraction``, which can take the following values:

            - `False` : display unit bases with negative powers as they are;
            - 'inline' or `True` : use a single-line fraction;
            - 'multiline' : use a multiline fraction (available for the
              'latex', 'console' and 'unicode' formats only; for others,
              an 'inline' fraction is used).

        Raises
        ------
        TypeError
            If ``format`` is of the wrong type.
        ValueError
            If ``format`` or ``fraction`` are not recognized.

        Examples
        --------
        >>> import astropy.units as u
        >>> kms = u.Unit('km / s')
        >>> kms.to_string()  # Generic uses fraction='inline' by default
        'km / s'
        >>> kms.to_string('latex')  # Latex uses fraction='multiline' by default
        '$\\mathrm{\\frac{km}{s}}$'
        >>> print(kms.to_string('unicode', fraction=False))
        km s⁻¹
        >>> print(kms.to_string('unicode', fraction='inline'))
        km / s
        >>> print(kms.to_string('unicode', fraction='multiline'))
        km
        ──
        s
        """
        from .format import get_format

        try:
            return get_format(format).to_string(self, **kwargs)
        except (TypeError, ValueError) as err:
            from .format import known_formats

            err.add_note(known_formats())
            raise err

    def __format__(self, format_spec: str) -> str:
        try:
            return self.to_string(format=format_spec)
        except ValueError:
            return format(str(self), format_spec)

    @staticmethod
    def _normalize_equivalencies(equivalencies):
        """Normalizes equivalencies, ensuring each is a 4-tuple.

        The resulting tuple is of the form::

            (from_unit, to_unit, forward_func, backward_func)

        Parameters
        ----------
        equivalencies : list of equivalency pairs, or None

        Returns
        -------
        A normalized list, including possible global defaults set by, e.g.,
        `set_enabled_equivalencies`, except when `equivalencies`=`None`,
        in which case the returned list is always empty.

        Raises
        ------
        ValueError if an equivalency cannot be interpreted
        """
        normalized = _normalize_equivalencies(equivalencies)
        if equivalencies is not None:
            normalized += get_current_unit_registry().equivalencies

        return normalized

    def __pow__(self, p: UnitPowerLike) -> CompositeUnit:
        try:  # Handling scalars should be as quick as possible
            return CompositeUnit(1, [self], [sanitize_power(p)], _error_check=False)
        except Exception:
            arr = np.asanyarray(p)
            p = arr.item(0)
            if (arr != p).any():
                raise ValueError(
                    "Quantities and Units may only be raised to a scalar power"
                ) from None
            return CompositeUnit(1, [self], [sanitize_power(p)], _error_check=False)

    @staticmethod
    def _warn_about_operation_with_deprecated_type(op: str, other: bytes | str) -> None:
        warnings.warn(
            AstropyDeprecationWarning(
                f"{op} involving a unit and a '{type(other).__name__}' instance are "
                f"deprecated since v7.1. Convert {other!r} to a unit explicitly."
            ),
            stacklevel=3,
        )

    def __truediv__(self, m):
        if isinstance(m, UnitBase):
            if m.is_unity():
                return self
            return CompositeUnit(1, [self, m], [1, -1], _error_check=False)

        if isinstance(m, (bytes, str)):
            self._warn_about_operation_with_deprecated_type("divisions", m)
            return self / Unit(m)

        try:
            # Cannot handle this as Unit, re-try as Quantity
            from .quantity import Quantity

            return Quantity(1, self) / m
        except TypeError:
            return NotImplemented

    def __rtruediv__(self, m):
        if isinstance(m, (bytes, str)):
            self._warn_about_operation_with_deprecated_type("divisions", m)
            return Unit(m) / self

        try:
            # Cannot handle this as Unit.  Here, m cannot be a Quantity,
            # so we make it into one, fasttracking when it does not have a
            # unit, for the common case of <array> / <unit>.
            from .quantity import Quantity

            if hasattr(m, "unit"):
                result = Quantity(m)
                result /= self
                return result
            else:
                return Quantity(m, self ** (-1))
        except TypeError:
            if isinstance(m, np.ndarray):
                raise
            return NotImplemented

    def __mul__(self, m):
        if isinstance(m, UnitBase):
            if m.is_unity():
                return self
            elif self.is_unity():
                return m
            return CompositeUnit(1, [self, m], [1, 1], _error_check=False)

        if isinstance(m, (bytes, str)):
            self._warn_about_operation_with_deprecated_type("products", m)
            return self * Unit(m)

        # Cannot handle this as Unit, re-try as Quantity.
        try:
            from .quantity import Quantity

            return Quantity(1, unit=self) * m
        except TypeError:
            return NotImplemented

    def __rmul__(self, m):
        if isinstance(m, (bytes, str)):
            self._warn_about_operation_with_deprecated_type("products", m)
            return Unit(m) * self

        # Cannot handle this as Unit.  Here, m cannot be a Quantity,
        # so we make it into one, fasttracking when it does not have a unit
        # for the common case of <array> * <unit>.
        try:
            from .quantity import Quantity

            if hasattr(m, "unit"):
                result = Quantity(m)
                result *= self
                return result
            else:
                return Quantity(m, unit=self)
        except TypeError:
            if isinstance(m, np.ndarray):
                raise
            return NotImplemented

    def __rlshift__(self, m):
        try:
            from .quantity import Quantity

            return Quantity(m, self, copy=COPY_IF_NEEDED, subok=True)
        except Exception:
            if isinstance(m, np.ndarray):
                raise
            return NotImplemented

    def __rrshift__(self, m):
        warnings.warn(
            ">> is not implemented. Did you mean to convert "
            f"to a Quantity with unit {m} using '<<'?",
            AstropyWarning,
        )
        return NotImplemented

    def __hash__(self) -> int:
        return self._hash

    @cached_property
    def _hash(self) -> int:
        return hash((self.scale, *[x.name for x in self.bases], *map(str, self.powers)))

    def __getstate__(self) -> dict[str, object]:
        # If we get pickled, we should *not* store the memoized members since
        # hashes of strings vary between sessions.
        state = self.__dict__.copy()
        state.pop("_hash", None)
        state.pop("_physical_type_id", None)
        return state

    def __eq__(self, other):
        if self is other:
            return True

        try:
            other = Unit(other, parse_strict="silent")
        except (ValueError, UnitsError, TypeError):
            return NotImplemented

        # Other is unit-like, but the test below requires it is a UnitBase
        # instance; if it is not, give up (so that other can try).
        if not isinstance(other, UnitBase):
            return NotImplemented

        try:
            return is_effectively_unity(self._to(other))
        except UnitsError:
            return False

    def __ne__(self, other):
        return not (self == other)

    def __le__(self, other):
        scale = self._to(Unit(other))
        return scale <= 1.0 or is_effectively_unity(scale)

    def __ge__(self, other):
        scale = self._to(Unit(other))
        return scale >= 1.0 or is_effectively_unity(scale)

    def __lt__(self, other):
        return not (self >= other)

    def __gt__(self, other):
        return not (self <= other)

    def __neg__(self) -> Quantity:
        return self * -1.0

    def is_equivalent(self, other, equivalencies=[]):
        """Check whether this unit is equivalent to ``other``.

        Parameters
        ----------
        other : `~astropy.units.Unit`, str, or tuple
            The unit to convert to. If a tuple of units is specified, this
            method returns true if the unit matches any of those in the tuple.

        equivalencies : list of tuple
            A list of equivalence pairs to try if the units are not
            directly convertible.  See :ref:`astropy:unit_equivalencies`.
            This list is in addition to possible global defaults set by, e.g.,
            `set_enabled_equivalencies`.
            Use `None` to turn off all equivalencies.

        Returns
        -------
        bool
        """
        equivalencies = self._normalize_equivalencies(equivalencies)

        if isinstance(other, tuple):
            return any(self.is_equivalent(u, equivalencies) for u in other)

        other = Unit(other, parse_strict="silent")

        return self._is_equivalent(other, equivalencies)

    def _is_equivalent(self, other, equivalencies=[]):
        """Returns `True` if this unit is equivalent to `other`.
        See `is_equivalent`, except that a proper Unit object should be
        given (i.e., no string) and that the equivalency list should be
        normalized using `_normalize_equivalencies`.
        """
        if isinstance(other, UnrecognizedUnit):
            return False

        if self._physical_type_id == other._physical_type_id:
            return True
        elif len(equivalencies):
            unit = self.decompose()
            other = other.decompose()
            for a, b, _, _ in equivalencies:
                if b is None:
                    # after canceling, is what's left convertible
                    # to dimensionless (according to the equivalency)?
                    try:
                        (other / unit).decompose([a])
                        return True
                    except Exception:
                        pass
                elif (a._is_equivalent(unit) and b._is_equivalent(other)) or (
                    b._is_equivalent(unit) and a._is_equivalent(other)
                ):
                    return True

        return False

    def _apply_equivalencies(self, unit, other, equivalencies):
        """
        Internal function (used from `get_converter`) to apply
        equivalence pairs.
        """

        def make_converter(scale1, func, scale2):
            def convert(v):
                return func(_condition_arg(v) / scale1) * scale2

            return convert

        for funit, tunit, a, b in equivalencies:
            if tunit is None:
                ratio = other.decompose() / unit.decompose()
                try:
                    ratio_in_funit = ratio.decompose([funit])
                    return make_converter(ratio_in_funit.scale, a, 1.0)
                except UnitsError:
                    pass
            else:
                try:
                    scale1 = funit._to(unit)
                    scale2 = tunit._to(other)
                    return make_converter(scale1, a, scale2)
                except UnitsError:
                    pass
                try:
                    scale1 = tunit._to(unit)
                    scale2 = funit._to(other)
                    return make_converter(scale1, b, scale2)
                except UnitsError:
                    pass

        def get_err_str(unit: UnitBase) -> str:
            unit_str = unit.to_string("generic")
            physical_type = unit.physical_type
            if physical_type != "unknown":
                unit_str = f"'{unit_str}' ({physical_type})"
            else:
                unit_str = f"'{unit_str}'"
            return unit_str

        unit_str = get_err_str(unit)
        other_str = get_err_str(other)

        raise UnitConversionError(f"{unit_str} and {other_str} are not convertible")

    def get_converter(self, other, equivalencies=[]):
        """
        Create a function that converts values from this unit to another.

        Parameters
        ----------
        other : unit-like
            The unit to convert to.
        equivalencies : list of tuple
            A list of equivalence pairs to try if the units are not
            directly convertible.  See :ref:`astropy:unit_equivalencies`.
            This list is in addition to possible global defaults set by, e.g.,
            `set_enabled_equivalencies`.
            Use `None` to turn off all equivalencies.

        Returns
        -------
        func : callable
            A callable that takes an array-like argument and returns
            it converted from units of self to units of other.

        Raises
        ------
        UnitsError
            If the units cannot be converted to each other.

        Notes
        -----
        This method is used internally in `Quantity` to convert to
        different units. Note that the function returned takes
        and returns values, not quantities.
        """
        # First see if it is just a scaling.
        try:
            scale = self._to(other)
        except UnitsError:
            pass
        else:
            if scale == 1.0:
                # If no conversion is necessary, returns ``unit_scale_converter``
                # (which is used as a check in quantity helpers).
                return unit_scale_converter
            else:
                return lambda val: scale * _condition_arg(val)

        # if that doesn't work, maybe we can do it with equivalencies?
        try:
            return self._apply_equivalencies(
                self, other, self._normalize_equivalencies(equivalencies)
            )
        except UnitsError as exc:
            # Last hope: maybe other knows how to do it?
            # We assume the equivalencies have the unit itself as first item.
            # TODO: maybe better for other to have a `_back_converter` method?
            if hasattr(other, "equivalencies"):
                for funit, tunit, _, b in other.equivalencies:
                    if other is funit:
                        try:
                            converter = self.get_converter(tunit, equivalencies)
                        except Exception:
                            pass
                        else:
                            return lambda v: b(converter(v))

            raise exc

    def _to(self, other: UnitBase) -> UnitScale:
        """
        Returns the scale to the specified unit.

        See `to`, except that a Unit object should be given (i.e., no
        string), and that all defaults are used, i.e., no
        equivalencies and value=1.
        """
        # There are many cases where we just want to ensure a Quantity is
        # of a particular unit, without checking whether it's already in
        # a particular unit.  If we're being asked to convert from a unit
        # to itself, we can short-circuit all of this.
        if self is other:
            return 1.0

        # Don't presume decomposition is possible; e.g.,
        # conversion to function units is through equivalencies.
        if isinstance(other, UnitBase):
            self_decomposed = self.decompose()
            other_decomposed = other.decompose()

            # Check quickly whether equivalent.  This is faster than
            # `is_equivalent`, because it doesn't generate the entire
            # physical type list of both units.  In other words it "fails
            # fast".
            if self_decomposed.powers == other_decomposed.powers and all(
                self_base is other_base
                for (self_base, other_base) in zip(
                    self_decomposed.bases, other_decomposed.bases
                )
            ):
                return self_decomposed.scale / other_decomposed.scale

        raise UnitConversionError(f"'{self!r}' is not a scaled version of '{other!r}'")

    def to(self, other, value=UNITY, equivalencies=[]):
        """
        Return the converted values in the specified unit.

        Parameters
        ----------
        other : unit-like
            The unit to convert to.

        value : int, float, or scalar array-like, optional
            Value(s) in the current unit to be converted to the
            specified unit.  If not provided, defaults to 1.0

        equivalencies : list of tuple
            A list of equivalence pairs to try if the units are not
            directly convertible.  See :ref:`astropy:unit_equivalencies`.
            This list is in addition to possible global defaults set by, e.g.,
            `set_enabled_equivalencies`.
            Use `None` to turn off all equivalencies.

        Returns
        -------
        values : scalar or array
            Converted value(s). Input value sequences are returned as
            numpy arrays.

        Raises
        ------
        UnitsError
            If units are inconsistent
        """
        if other is self and value is UNITY:
            return UNITY
        else:
            return self.get_converter(Unit(other), equivalencies)(value)

    @deprecated(since="7.0", alternative="to()")
    def in_units(self, other, value=1.0, equivalencies=[]):
        """
        Alias for `to` for backward compatibility with pynbody.
        """
        return self.to(other, value=value, equivalencies=equivalencies)

    def decompose(self, bases: Collection[UnitBase] = ()) -> UnitBase:
        """
        Return a unit object composed of only irreducible units.

        Parameters
        ----------
        bases : sequence of UnitBase, optional
            The bases to decompose into.  When not provided,
            decomposes down to any irreducible units.  When provided,
            the decomposed result will only contain the given units.
            This will raises a `UnitsError` if it's not possible
            to do so.

        Returns
        -------
        unit : `~astropy.units.CompositeUnit`
            New object containing only irreducible unit objects.
        """
        raise NotImplementedError()

    def _compose(
        self, equivalencies=[], namespace=[], max_depth=2, depth=0, cached_results=None
    ):
        def is_final_result(unit: UnitBase) -> bool:
            # Returns True if this result contains only the expected
            # units
            return all(base in namespace for base in unit.bases)

        unit = self.decompose()

        cached = cached_results.get(unit)
        if cached is not None:
            if isinstance(cached, Exception):
                raise cached
            return cached

        # Prevent too many levels of recursion
        # And special case for dimensionless unit
        if depth >= max_depth:
            cached_results[unit] = [unit]
            return [unit]

        # Make a list including all of the equivalent units
        units = [unit]
        for funit, tunit, a, b in equivalencies:
            if tunit is not None:
                if self._is_equivalent(funit):
                    scale = funit.decompose().scale / unit.scale
                    units.append(Unit(a(1.0 / scale) * tunit).decompose())
                elif self._is_equivalent(tunit):
                    scale = tunit.decompose().scale / unit.scale
                    units.append(Unit(b(1.0 / scale) * funit).decompose())
            else:
                if self._is_equivalent(funit):
                    units.append(Unit(unit.scale))

        # Store partial results
        partial_results = []
        # Store final results that reduce to a single unit or pair of
        # units
        if len(unit.bases) == 0:
            final_results = [{unit}, set()]
        else:
            final_results = [set(), set()]

        for tunit in namespace:
            tunit_decomposed = tunit.decompose()
            for u in units:
                # If the unit is a base unit, look for an exact match
                # to one of the bases of the target unit.  If found,
                # factor by the same power as the target unit's base.
                # This allows us to factor out fractional powers
                # without needing to do an exhaustive search.
                if len(tunit_decomposed.bases) == 1:
                    for base, power in zip(u.bases, u.powers):
                        if tunit_decomposed._is_equivalent(base):
                            tunit = tunit**power
                            tunit_decomposed = tunit_decomposed**power
                            break

                composed = (u / tunit_decomposed).decompose()
                factored = composed * tunit
                len_bases = len(composed.bases)
                if is_final_result(factored) and len_bases <= 1:
                    final_results[len_bases].add(factored)
                else:
                    partial_results.append((len_bases, composed, tunit))

        # Do we have any minimal results?
        for final_result in final_results:
            if len(final_result):
                results = final_results[0].union(final_results[1])
                cached_results[unit] = results
                return results

        partial_results.sort(key=operator.itemgetter(0))

        # ...we have to recurse and try to further compose
        results = []
        for _, composed, tunit in partial_results:
            try:
                composed_list = composed._compose(
                    equivalencies=equivalencies,
                    namespace=namespace,
                    max_depth=max_depth,
                    depth=depth + 1,
                    cached_results=cached_results,
                )
            except UnitsError:
                composed_list = []
            for subcomposed in composed_list:
                results.append((len(subcomposed.bases), subcomposed, tunit))

        if results:
            results.sort(key=operator.itemgetter(0))

            min_length = results[0][0]
            subresults = set()
            for len_bases, composed, tunit in results:
                if len_bases > min_length:
                    break

                factored = composed * tunit
                if is_final_result(factored):
                    subresults.add(factored)

            if subresults:
                cached_results[unit] = subresults
                return subresults

        if not is_final_result(self):
            result = UnitsError(
                f"Cannot represent unit {self} in terms of the given units"
            )
            cached_results[unit] = result
            raise result

        cached_results[unit] = [self]
        return [self]

    def compose(
        self, equivalencies=[], units=None, max_depth=2, include_prefix_units=None
    ):
        """
        Return the simplest possible composite unit(s) that represent
        the given unit.  Since there may be multiple equally simple
        compositions of the unit, a list of units is always returned.

        Parameters
        ----------
        equivalencies : list of tuple
            A list of equivalence pairs to also list.  See
            :ref:`astropy:unit_equivalencies`.
            This list is in addition to possible global defaults set by, e.g.,
            `set_enabled_equivalencies`.
            Use `None` to turn off all equivalencies.

        units : set of `~astropy.units.Unit`, optional
            If not provided, any known units may be used to compose
            into.  Otherwise, ``units`` is a dict, module or sequence
            containing the units to compose into.

        max_depth : int, optional
            The maximum recursion depth to use when composing into
            composite units.

        include_prefix_units : bool, optional
            When `True`, include prefixed units in the result.
            Default is `True` if a sequence is passed in to ``units``,
            `False` otherwise.

        Returns
        -------
        units : list of `CompositeUnit`
            A list of candidate compositions.  These will all be
            equally simple, but it may not be possible to
            automatically determine which of the candidates are
            better.
        """
        # if units parameter is specified and is a sequence (list|tuple),
        # include_prefix_units is turned on by default.  Ex: units=[u.kpc]
        if include_prefix_units is None:
            include_prefix_units = isinstance(units, (list, tuple))

        # Pre-normalize the equivalencies list
        equivalencies = self._normalize_equivalencies(equivalencies)

        # The namespace of units to compose into should be filtered to
        # only include units with bases in common with self, otherwise
        # they can't possibly provide useful results.  Having too many
        # destination units greatly increases the search space.

        def has_bases_in_common(a: UnitBase, b: UnitBase) -> bool:
            return any(ab in b.bases for ab in a.bases) if a.bases or b.bases else True

        def has_bases_in_common_with_equiv(unit: UnitBase, other: UnitBase) -> bool:
            if has_bases_in_common(unit, other):
                return True
            for funit, tunit, _, _ in equivalencies:
                if tunit is not None:
                    if unit._is_equivalent(funit):
                        if has_bases_in_common(tunit.decompose(), other):
                            return True
                    elif unit._is_equivalent(tunit):
                        if has_bases_in_common(funit.decompose(), other):
                            return True
                else:
                    if unit._is_equivalent(funit):
                        if has_bases_in_common(dimensionless_unscaled, other):
                            return True
            return False

        def filter_units(units: Iterable[object]) -> set[UnitBase]:
            return {
                tunit
                for tunit in units
                if (
                    isinstance(tunit, UnitBase)
                    and (include_prefix_units or not isinstance(tunit, PrefixUnit))
                    and has_bases_in_common_with_equiv(decomposed, tunit.decompose())
                )
            }

        decomposed = self.decompose()

        if units is None:
            units = (
                filter_units(self._get_units_with_same_physical_type(equivalencies))
                or get_current_unit_registry().non_prefix_units
            )
        else:
            units = filter_units(_flatten_units_collection(units))

        # Sort the results so the simplest ones appear first.
        # Simplest is defined as "the minimum sum of absolute
        # powers" (i.e. the fewest bases), and preference should
        # be given to results where the sum of powers is positive
        # and the scale is exactly equal to 1.0
        return sorted(
            self._compose(
                equivalencies=equivalencies,
                namespace=units,
                max_depth=max_depth,
                depth=0,
                cached_results={},
            ),
            key=lambda x: (
                not is_effectively_unity(x.scale),
                sum(x.powers) < 0.0,
                sum(map(abs, x.powers)),
                abs(x.scale),
            ),
        )

    def to_system(self, system):
        """Convert this unit into ones belonging to the given system.

        Since more than one result may be possible, a list is always
        returned.

        Parameters
        ----------
        system : module
            The module that defines the unit system.  Commonly used
            ones include `astropy.units.si` and `astropy.units.cgs`.

            To use your own module it must contain unit objects and a
            sequence member named ``bases`` containing the base units of
            the system.

        Returns
        -------
        units : list of `CompositeUnit`
            With an attempt to sort simpler units to the start (see examples).

        Examples
        --------
        >>> import astropy.units as u
        >>> (u.N / u.m**2).to_system(u.si)  # preference for simpler units
        [Unit("Pa"), Unit("N / m2"), Unit("J / m3")]
        >>> u.Pa.to_system(u.cgs)
        [Unit("10 Ba"), Unit("10 P / s")]
        >>> u.Ba.to_system(u.si)
        [Unit("0.1 Pa"), Unit("0.1 N / m2"), Unit("0.1 J / m3")]
        >>> (u.AU/u.yr).to_system(u.cgs)  # preference for base units
        [Unit("474047 cm / s"), Unit("474047 Gal s")]
        >>> (u.m / u.s**2).to_system(u.cgs)
        [Unit("100 cm / s2"), Unit("100 Gal")]

        """
        return sorted(
            self.decompose(bases=system.bases).compose(units=system),
            key=lambda x: len(set(x.bases).difference(system.bases)),
        )

    @lazyproperty
    def si(self) -> UnitBase:
        """The unit expressed in terms of SI units."""
        from . import si

        return self.to_system(si)[0]

    @lazyproperty
    def cgs(self) -> UnitBase:
        """The unit expressed in terms of CGS units."""
        from . import cgs

        return self.to_system(cgs)[0]

    @property
    def physical_type(self) -> PhysicalType:
        """
        Physical type(s) dimensionally compatible with the unit.

        Returns
        -------
        `~astropy.units.physical.PhysicalType`
            A representation of the physical type(s) of a unit.

        Examples
        --------
        >>> from astropy import units as u
        >>> u.m.physical_type
        PhysicalType('length')
        >>> (u.m ** 2 / u.s).physical_type
        PhysicalType({'diffusivity', 'kinematic viscosity'})

        Physical types can be compared to other physical types
        (recommended in packages) or to strings.

        >>> area = (u.m ** 2).physical_type
        >>> area == u.m.physical_type ** 2
        True
        >>> area == "area"
        True

        `~astropy.units.physical.PhysicalType` objects can be used for
        dimensional analysis.

        >>> number_density = u.m.physical_type ** -3
        >>> velocity = (u.m / u.s).physical_type
        >>> number_density * velocity
        PhysicalType('particle flux')
        """
        from . import physical

        return physical.get_physical_type(self)

    def _get_units_with_same_physical_type(self, equivalencies=[]):
        """
        Return a list of registered units with the same physical type
        as this unit.

        This function is used by Quantity to add its built-in
        conversions to equivalent units.

        This is a private method, since end users should be encouraged
        to use the more powerful `compose` and `find_equivalent_units`
        methods (which use this under the hood).

        Parameters
        ----------
        equivalencies : list of tuple
            A list of equivalence pairs to also pull options from.
            See :ref:`astropy:unit_equivalencies`.  It must already be
            normalized using `_normalize_equivalencies`.
        """
        unit_registry = get_current_unit_registry()
        units = set(unit_registry.get_units_with_physical_type(self))
        for funit, tunit, _, _ in equivalencies:
            if tunit is not None:
                if self.is_equivalent(funit) and tunit not in units:
                    units.update(unit_registry.get_units_with_physical_type(tunit))
                if self._is_equivalent(tunit) and funit not in units:
                    units.update(unit_registry.get_units_with_physical_type(funit))
            else:
                if self.is_equivalent(funit):
                    units.add(dimensionless_unscaled)
        return units

    class EquivalentUnitsList(list):
        """
        A class to handle pretty-printing the result of
        `find_equivalent_units`.
        """

        HEADING_NAMES: Final[tuple[str, str, str]] = (
            "Primary name",
            "Unit definition",
            "Aliases",
        )
        NO_EQUIV_UNITS_MSG: Final[str] = "There are no equivalent units"

        def __repr__(self) -> str:
            if not self:
                return self.NO_EQUIV_UNITS_MSG
            lines = self._process_units()
            widths = list(map(len, self.HEADING_NAMES))
            for line in lines:
                widths = [max(w, len(col)) for w, col in zip(widths, line, strict=True)]
            row_template = "  {{0:<{}s}} | {{1:<{}s}} | {{2:<{}s}}".format(*widths)
            return "\n".join(
                [
                    row_template.format(*self.HEADING_NAMES),
                    "[",
                    *(f"{row_template.format(*line)} ," for line in lines),
                    "]",
                ]
            )

        def _repr_html_(self) -> str:
            """
            Outputs a HTML table representation within Jupyter notebooks.
            """
            if not self:
                return f"<p>{self.NO_EQUIV_UNITS_MSG}</p>"
            heading = "".join(f"<th>{name}</th>" for name in self.HEADING_NAMES)
            rows = (
                f"<tr>{''.join(f'<td>{elem}</td>' for elem in row)}</tr>"
                for row in self._process_units()
            )
            # The HTML will be rendered & the table is simple, so don't
            # bother to include newlines & indentation for the HTML code.
            return f'<table style="width:50%"><tr>{heading}</tr>{"".join(rows)}</table>'

        def _process_units(self) -> list[tuple[str, str, str]]:
            """
            Extract attributes, and sort, the equivalent units pre-formatting.
            """
            return sorted(
                (
                    unit.name,
                    "irreducible" if (s := str(unit.decompose())) == unit.name else s,
                    ", ".join(unit.aliases),
                )
                for unit in self
            )

    def find_equivalent_units(
        self, equivalencies=[], units=None, include_prefix_units=False
    ):
        """
        Return a list of all the units that are the same type as ``self``.

        Parameters
        ----------
        equivalencies : list of tuple
            A list of equivalence pairs to also list.  See
            :ref:`astropy:unit_equivalencies`.
            Any list given, including an empty one, supersedes global defaults
            that may be in effect (as set by `set_enabled_equivalencies`)

        units : set of `~astropy.units.Unit`, optional
            If not provided, all defined units will be searched for
            equivalencies.  Otherwise, may be a dict, module or
            sequence containing the units to search for equivalencies.

        include_prefix_units : bool, optional
            When `True`, include prefixed units in the result.
            Default is `False`.

        Returns
        -------
        units : list of `UnitBase`
            A list of unit objects that match ``u``.  A subclass of
            `list` (``EquivalentUnitsList``) is returned that
            pretty-prints the list of units when output.
        """
        results = self.compose(
            equivalencies=equivalencies,
            units=units,
            max_depth=1,
            include_prefix_units=include_prefix_units,
        )
        results = {
            x.bases[0] for x in results if len(x.bases) == 1 and x.powers[0] == 1
        }
        return self.EquivalentUnitsList(results)

    def is_unity(self) -> bool:
        """Check whether the unit is unscaled and dimensionless."""
        return False


class NamedUnit(UnitBase):
    """
    The base class of units that have a name.

    Parameters
    ----------
    st : str, list of str, 2-tuple
        The name of the unit.  If a list of strings, the first element
        is the canonical (short) name, and the rest of the elements
        are aliases.  If a tuple of lists, the first element is a list
        of short names, and the second element is a list of long
        names; all but the first short name are considered "aliases".
        Each name *should* be a valid Python identifier to make it
        easy to access, but this is not required.

    namespace : dict, optional
        When provided, inject the unit, and all of its aliases, in the
        given namespace dictionary.  If a unit by the same name is
        already in the namespace, a ValueError is raised.

    doc : str, optional
        A docstring describing the unit.

    format : dict, optional
        A mapping to format-specific representations of this unit.
        For example, for the ``Ohm`` unit, it might be nice to have it
        displayed as ``\\Omega`` by the ``latex`` formatter.  In that
        case, `format` argument should be set to::

            {'latex': r'\\Omega'}

    Raises
    ------
    ValueError
        If any of the given unit names are already in the registry.

    ValueError
        If any of the given unit names are not valid Python tokens.
    """

    def __init__(
        self,
        st: str | list[str] | tuple[list[str], list[str]],
        doc: str | None = None,
        format: Mapping[str, str] | None = None,
        namespace: MutableMapping[str, object] | None = None,
    ) -> None:
        if isinstance(st, (bytes, str)):
            self._names = [st]
            self._short_names = [st]
            self._long_names = []
        elif isinstance(st, tuple):
            if not len(st) == 2:
                raise ValueError("st must be string, list or 2-tuple")
            self._names = st[0] + [n for n in st[1] if n not in st[0]]
            if not len(self._names):
                raise ValueError("must provide at least one name")
            self._short_names = st[0][:]
            self._long_names = st[1][:]
        else:
            if len(st) == 0:
                raise ValueError("st list must have at least one entry")
            self._names = st[:]
            self._short_names = [st[0]]
            self._long_names = st[1:]

        self._format = {} if format is None else format
        self.__doc__ = (
            self._generate_doc() if doc is None else textwrap.fill(textwrap.dedent(doc))
        )
        self._inject(namespace)

    def _generate_doc(self) -> str:
        """
        Generate a docstring for the unit if the user didn't supply
        one.  This is only used from the constructor and may be
        overridden in subclasses.
        """
        names = self.names
        if len(self.names) > 1:
            return f"{names[1]} ({names[0]})"
        else:
            return names[0]

    @deprecated(since="7.0", alternative="to_string()")
    def get_format_name(self, format):
        """
        Get a name for this unit that is specific to a particular
        format.

        Uses the dictionary passed into the `format` kwarg in the
        constructor.

        Parameters
        ----------
        format : str
            The name of the format

        Returns
        -------
        name : str
            The name of the unit for the given format.
        """
        return self._get_format_name(format)

    def _get_format_name(self, format: str) -> str:
        return self._format.get(format, self.name)

    @property
    def names(self) -> list[str]:
        """All the names associated with the unit."""
        return self._names

    @property
    def name(self) -> str:
        """The canonical (short) name associated with the unit."""
        return self._names[0]

    @property
    def aliases(self) -> list[str]:
        """The aliases (long) names for the unit."""
        return self._names[1:]

    @property
    def short_names(self) -> list[str]:
        """All the short names associated with the unit."""
        return self._short_names

    @property
    def long_names(self) -> list[str]:
        """All the long names associated with the unit."""
        return self._long_names

    def _inject(self, namespace: MutableMapping[str, object] | None = None) -> None:
        """
        Injects the unit, and all of its aliases, in the given
        namespace dictionary.
        """
        if namespace is None:
            return

        # Loop through all of the names first, to ensure all of them
        # are new, then add them all as a single "transaction" below.
        for name in (unicodedata.normalize("NFKC", name) for name in self._names):
            if name in namespace and self != namespace[name]:
                raise ValueError(
                    f"Object with NFKC normalized name {name!r} already exists in "
                    f"given namespace ({namespace[name]!r})."
                )

        for name in self._names:
            namespace[name] = self


def _recreate_irreducible_unit(cls, names, registered):
    """
    This is used to reconstruct units when passed around by
    multiprocessing.
    """
    registry = get_current_unit_registry().registry
    if names[0] in registry:
        # If in local registry return that object.
        return registry[names[0]]
    else:
        # otherwise, recreate the unit.
        unit = cls(names)
        if registered:
            # If not in local registry but registered in origin registry,
            # enable unit in local registry.
            get_current_unit_registry().add_enabled_units([unit])

        return unit


class IrreducibleUnit(NamedUnit):
    """
    Irreducible units are the units that all other units are defined
    in terms of.

    Examples are meters, seconds, kilograms, amperes, etc.  There is
    only once instance of such a unit per type.
    """

    def __reduce__(self):
        # When IrreducibleUnit objects are passed to other processes
        # over multiprocessing, they need to be recreated to be the
        # ones already in the subprocesses' namespace, not new
        # objects, or they will be considered "unconvertible".
        # Therefore, we have a custom pickler/unpickler that
        # understands how to recreate the Unit on the other side.
        registry = get_current_unit_registry().registry
        return (
            _recreate_irreducible_unit,
            (self.__class__, list(self.names), self.name in registry),
            self.__getstate__(),
        )

    @property
    def represents(self) -> Self:
        """The unit that this named unit represents.

        For an irreducible unit, that is always itself.
        """
        return self

    def decompose(self, bases: Collection[UnitBase] = ()) -> UnitBase:
        if len(bases) and self not in bases:
            for base in bases:
                try:
                    scale = self._to(base)
                except UnitsError:
                    pass
                else:
                    if is_effectively_unity(scale):
                        return base
                    else:
                        return CompositeUnit(scale, [base], [1], _error_check=False)

            raise UnitConversionError(
                f"Unit {self} can not be decomposed into the requested bases"
            )

        return self


class UnrecognizedUnit(IrreducibleUnit):
    """
    A unit that did not parse correctly.  This allows for
    round-tripping it as a string, but no unit operations actually work
    on it.

    Parameters
    ----------
    st : str
        The name of the unit.
    """

    # For UnrecognizedUnits, we want to use "standard" Python
    # pickling, not the special case that is used for
    # IrreducibleUnits.
    __reduce__ = object.__reduce__

    def __repr__(self) -> str:
        return f"UnrecognizedUnit({self})"

    def __bytes__(self) -> bytes:
        return self.name.encode("ascii", "replace")

    def __str__(self) -> str:
        return self.name

    def to_string(self, format=None):
        return self.name

    def _unrecognized_operator(self, *args, **kwargs):
        raise ValueError(
            f"The unit {self.name!r} is unrecognized, so all arithmetic operations "
            "with it are invalid."
        )

    __pow__ = __truediv__ = __rtruediv__ = __mul__ = __rmul__ = _unrecognized_operator
    __lt__ = __gt__ = __le__ = __ge__ = __neg__ = _unrecognized_operator

    def __eq__(self, other):
        try:
            other = Unit(other, parse_strict="silent")
        except (ValueError, UnitsError, TypeError):
            return NotImplemented

        return isinstance(other, type(self)) and self.name == other.name

    def __ne__(self, other):
        return not (self == other)

    def is_equivalent(self, other, equivalencies=None):
        self._normalize_equivalencies(equivalencies)
        return self == other

    def get_converter(self, other, equivalencies=None):
        self._normalize_equivalencies(equivalencies)
        raise ValueError(
            f"The unit {self.name!r} is unrecognized.  It can not be converted "
            "to other units."
        )

    def _get_format_name(self, format: str) -> str:
        return self.name

    def is_unity(self) -> Literal[False]:
        return False


class _UnitMetaClass(type):
    """
    This metaclass exists because the Unit constructor should
    sometimes return instances that already exist.  This "overrides"
    the constructor before the new instance is actually created, so we
    can return an existing one.
    """

    def __call__(
        cls,
        s="",
        represents=None,
        format=None,
        namespace=None,
        doc=None,
        parse_strict="raise",
    ):
        # Short-circuit if we're already a unit
        if hasattr(s, "_physical_type_id"):
            return s

        if represents is not None:
            # This has the effect of calling the real __new__ and
            # __init__ on the Unit class.
            return super().__call__(
                s, represents, format=format, namespace=namespace, doc=doc
            )

        if isinstance(s, (str, bytes)):
            if len(s.strip()) == 0:
                # Return the NULL unit
                return dimensionless_unscaled

            from .format import Generic, get_format

            try:
                f = get_format(format)
            except (TypeError, ValueError) as err:
                from .format import known_parsers

                err.add_note(known_parsers())
                raise err
            if isinstance(s, bytes):
                s = s.decode("ascii")

            try:
                return f._validate_unit(s, detailed_exception=False)  # Try a shortcut
            except (AttributeError, ValueError):
                # No `f._validate_unit()` (AttributeError)
                # or `s` was a composite unit (ValueError).
                pass

            try:
                with (
                    _WARNING_LOCK,
                    warnings.catch_warnings(
                        action=_WARNING_ACTIONS[parse_strict],
                        category=UnitParserWarning,
                    ),
                ):
                    return f.parse(s)
            except NotImplementedError:
                raise
            except UnitParserWarning as err:
                new_err = ValueError(err)
                new_err.add_note(
                    "If you cannot change the unit string then try specifying the "
                    "'parse_strict' argument."
                )
                raise new_err from err
            except KeyError as err:
                if parse_strict in _WARNING_ACTIONS:
                    raise
                raise ValueError(
                    "'parse_strict' must be 'warn', 'raise' or 'silent'"
                ) from None
            except Exception as e:
                if parse_strict != "silent":
                    # Deliberately not issubclass here. Subclasses
                    # should use their name.
                    format_clause = "" if f is Generic else f.name + " "
                    msg = (
                        f"'{s}' did not parse as {format_clause}unit: {str(e)} "
                        "If this is meant to be a custom unit, "
                        "define it with 'u.def_unit'. To have it "
                        "recognized inside a file reader or other code, "
                        "enable it with 'u.add_enabled_units'. "
                        "For details, see "
                        "https://docs.astropy.org/en/latest/units/combining_and_defining.html"
                    )
                    if parse_strict == "raise":
                        raise ValueError(msg)
                    warnings.warn(msg, UnitsWarning)
                return UnrecognizedUnit(s)

        from .quantity import Quantity

        if isinstance(s, Quantity):
            if is_effectively_unity(s.value):
                return s.unit
            return CompositeUnit(
                sanitize_scale(s.value) * s.unit.scale,
                bases=s.unit.bases,
                powers=s.unit.powers,
                _error_check=False,
            )

        from .typing import UnitScaleLike

        if isinstance(s, UnitScaleLike):
            return CompositeUnit(s, [], [])

        if isinstance(s, tuple):
            from .structured import StructuredUnit

            return StructuredUnit(s)

        raise TypeError(f"{s!r} cannot be converted to a Unit")


class Unit(NamedUnit, metaclass=_UnitMetaClass):
    """
    The main unit class.

    There are a number of different ways to construct a Unit, but
    always returns a `UnitBase` instance.  If the arguments refer to
    an already-existing unit, that existing unit instance is returned,
    rather than a new one.

    - From a string::

        Unit(s, format=None, parse_strict='silent')

      Construct from a string representing a (possibly compound) unit.

      The optional `format` keyword argument specifies the format the
      string is in, by default ``"generic"``.  For a description of
      the available formats, see `astropy.units.format`.

      The optional ``parse_strict`` keyword argument controls what happens
      when the string does not comply with the specified format. It may be
      one of the following:

         - ``'raise'``: (default) raise a `ValueError` exception.

         - ``'warn'``: emit a `UnitParserWarning`, and return a unit.

         - ``'silent'``: return a unit silently.

      With ``'warn'`` or ``'silent'`` the parser might be able to parse the
      string and return a normal unit, but if it fails then an
      `UnrecognizedUnit` instance is returned.

    - From a number::

        Unit(number)

      Creates a dimensionless unit.

    - From a `UnitBase` instance::

        Unit(unit)

      Returns the given unit unchanged.

    - From no arguments::

        Unit()

      Returns the dimensionless unit.

    - The last form, which creates a new `Unit` is described in detail
      below.

    See also: https://docs.astropy.org/en/stable/units/

    Parameters
    ----------
    st : str or list of str
        The name of the unit.  If a list, the first element is the
        canonical (short) name, and the rest of the elements are
        aliases.

    represents : unit-like, optional
        The unit that this named unit represents.

    doc : str, optional
        A docstring describing the unit.

    format : dict, optional
        A mapping to format-specific representations of this unit.
        For example, for the ``Ohm`` unit, it might be nice to have it
        displayed as ``\\Omega`` by the ``latex`` formatter.  In that
        case, `format` argument should be set to::

            {'latex': r'\\Omega'}

    namespace : dict, optional
        When provided, inject the unit (and all of its aliases) into
        the given namespace.

    Raises
    ------
    ValueError
        If any of the given unit names are already in the registry.

    ValueError
        If any of the given unit names are not valid Python tokens.

    ValueError
        If ``represents`` cannot be parsed as a unit, e.g., because it is
        a malformed string or a |Quantity| that is not a scalar.

    """

    def __init__(self, st, represents=None, doc=None, format=None, namespace=None):
        represents = Unit(represents)
        self._represents = represents

        NamedUnit.__init__(self, st, namespace=namespace, doc=doc, format=format)

    @property
    def represents(self) -> UnitBase:
        """The unit that this named unit represents."""
        return self._represents

    def decompose(self, bases: Collection[UnitBase] = ()) -> UnitBase:
        return self._represents.decompose(bases=bases)

    def is_unity(self) -> bool:
        return self._represents.is_unity()

    @cached_property
    def _hash(self) -> int:
        return hash((self.name, self._represents))

    @classmethod
    def _from_physical_type_id(cls, physical_type_id: PhysicalTypeID) -> UnitBase:
        if len(physical_type_id) == 1 and physical_type_id[0][1] == 1:
            return cls(physical_type_id[0][0])
        # get string bases and powers from the ID tuple
        bases = [cls(base) for base, _ in physical_type_id]
        powers = [power for _, power in physical_type_id]
        return CompositeUnit(1, bases, powers, _error_check=False)


class PrefixUnit(Unit):
    """
    A unit that is simply a SI-prefixed version of another unit.

    For example, ``mm`` is a `PrefixUnit` of ``.001 * m``.

    The constructor is the same as for `Unit`.
    """


class CompositeUnit(UnitBase):
    """
    Create a composite unit using expressions of previously defined
    units.

    Direct use of this class is not recommended. Instead use the
    factory function `Unit` and arithmetic operators to compose
    units.

    Parameters
    ----------
    scale : number
        A scaling factor for the unit.

    bases : sequence of `UnitBase`
        A sequence of units this unit is composed of.

    powers : sequence of numbers
        A sequence of powers (in parallel with ``bases``) for each
        of the base units.

    Raises
    ------
    UnitScaleError
        If the scale is zero.
    """

    _decomposed_cache: CompositeUnit | None = None

    # _error_check can switch off runtime validation of scale, bases and powers.
    # These overloads enable type checkers to validate statically.
    @overload
    def __init__(
        self,
        scale: UnitScaleLike,
        bases: Sequence[UnitBase],
        powers: Sequence[UnitPowerLike],
        decompose: bool = False,
        decompose_bases: Collection[UnitBase] = (),
        _error_check: Literal[True] = True,
    ) -> None: ...
    @overload
    def __init__(
        self,
        scale: UnitScale,
        bases: Sequence[UnitBase],
        powers: Sequence[UnitPower],
        decompose: bool = False,
        decompose_bases: Collection[UnitBase] = (),
        _error_check: Literal[False] = False,
    ) -> None: ...

    def __init__(
        self,
        scale,
        bases,
        powers,
        decompose=False,
        decompose_bases=(),
        _error_check=True,
    ):
        # There are many cases internal to astropy.units where we
        # already know that all the bases are Unit objects, and the
        # powers have been validated.  In those cases, we can skip the
        # error checking for performance reasons.  When the private
        # kwarg `_error_check` is False, the error checking is turned
        # off.
        if _error_check:
            scale = sanitize_scale(scale)
            for base in bases:
                if not isinstance(base, UnitBase):
                    raise TypeError("bases must be sequence of UnitBase instances")
            powers = [sanitize_power(p) for p in powers]

        if not decompose and len(bases) == 1 and powers[0] >= 0:
            # Short-cut; with one unit there's nothing to expand and gather,
            # as that has happened already when creating the unit.  But do only
            # positive powers, since for negative powers we need to re-sort.
            unit = bases[0]
            power = powers[0]
            if power == 1:
                scale *= unit.scale
                self._bases = unit.bases
                self._powers = unit.powers
            elif power == 0:
                self._bases = []
                self._powers = []
            else:
                scale *= unit.scale**power
                self._bases = unit.bases
                self._powers = [
                    sanitize_power(operator.mul(*resolve_fractions(p, power)))
                    for p in unit.powers
                ]

            self._scale = sanitize_scale(scale)
        else:
            # Regular case: use inputs as preliminary scale, bases, and powers,
            # then "expand and gather" identical bases, sanitize the scale, &c.
            self._scale = scale
            self._bases = bases
            self._powers = powers
            self._expand_and_gather(decompose=decompose, bases=decompose_bases)

    def __repr__(self) -> str:
        if len(self._bases):
            return super().__repr__()
        else:
            if self._scale != 1.0:
                return f"Unit(dimensionless with a scale of {self._scale})"
            else:
                return "Unit(dimensionless)"

    @property
    def scale(self) -> UnitScale:
        """The scale of the composite unit."""
        return self._scale

    @property
    def bases(self) -> list[NamedUnit]:
        """The bases of the composite unit."""
        return self._bases

    @property
    def powers(self) -> list[UnitPower]:
        """The powers of the bases of the composite unit."""
        return self._powers

    def _expand_and_gather(
        self, decompose: bool = False, bases: Collection[UnitBase] = ()
    ):
        def add_unit(unit, power, scale):
            if bases and unit not in bases:
                for base in bases:
                    try:
                        scale *= unit._to(base) ** power
                    except UnitsError:
                        pass
                    else:
                        unit = base
                        break

            if unit in new_parts:
                a, b = resolve_fractions(new_parts[unit], power)
                new_parts[unit] = a + b
            else:
                new_parts[unit] = power
            return scale

        new_parts = {}
        scale = self._scale

        for b, p in zip(self._bases, self._powers):
            if decompose and b not in bases:
                b = b.decompose(bases=bases)

            if isinstance(b, CompositeUnit):
                scale *= b._scale**p
                for b_sub, p_sub in zip(b._bases, b._powers):
                    a, b = resolve_fractions(p_sub, p)
                    scale = add_unit(b_sub, a * b, scale)
            else:
                scale = add_unit(b, p, scale)

        new_parts = [x for x in new_parts.items() if x[1] != 0]
        new_parts.sort(key=lambda x: (-x[1], getattr(x[0], "name", "")))

        self._bases = [x[0] for x in new_parts]
        self._powers = [sanitize_power(x[1]) for x in new_parts]
        self._scale = sanitize_scale(scale)

    def __copy__(self) -> CompositeUnit:
        return CompositeUnit(self._scale, self._bases[:], self._powers[:])

    def decompose(self, bases: Collection[UnitBase] = ()) -> CompositeUnit:
        if len(bases) == 0 and self._decomposed_cache is not None:
            return self._decomposed_cache

        for base in self.bases:
            if not isinstance(base, IrreducibleUnit) or (
                len(bases) and base not in bases
            ):
                break
        else:
            if len(bases) == 0:
                self._decomposed_cache = self
            return self

        x = CompositeUnit(
            self.scale, self.bases, self.powers, decompose=True, decompose_bases=bases
        )
        if len(bases) == 0:
            self._decomposed_cache = x
        return x

    def is_unity(self) -> bool:
        unit = self.decompose()
        return len(unit.bases) == 0 and unit.scale == 1.0


class UnitPrefix(NamedTuple):
    """Prefix for representing multiples or sub-multiples of units.

    Parameters
    ----------
    symbols : tuple of str
        The symbols of the prefix, to be combined with symbols of units.
        If multiple are specified then they will be treated as aliases.
    names : tuple of str
        The names of the prefix, to be combined with names of units.
        If multiple are specified then they will be treated as aliases.
    factor : `~astropy.units.typing.UnitScale`
        The multiplicative factor represented by the prefix.

    Examples
    --------
    >>> UnitPrefix(("k",), ("kilo",), 1e3)  # Simple prefix
    UnitPrefix(symbols=('k',), names=('kilo',), factor=1000.0)
    >>> UnitPrefix(("da",), ("deca", "deka"), 1e1)  # Multiple names
    UnitPrefix(symbols=('da',), names=('deca', 'deka'), factor=10.0)
    >>> UnitPrefix(("u", "\N{MICRO SIGN}"), ("micro",), 1e-6)  # Multiple symbols
    UnitPrefix(symbols=('u', 'µ'), names=('micro',), factor=1e-06)
    """

    symbols: tuple[str, ...]
    "The symbols of the prefix, to be combined with symbols of units."
    names: tuple[str, ...]
    "The names of the prefix, to be combined with names of units."
    factor: UnitScale
    "The multiplicative factor represented by the prefix."


si_prefixes: Final = tuple(
    UnitPrefix(symbols, names, factor)
    for symbols, names, factor in (
        (("Q",), ("quetta",), 1e30),
        (("R",), ("ronna",), 1e27),
        (("Y",), ("yotta",), 1e24),
        (("Z",), ("zetta",), 1e21),
        (("E",), ("exa",), 1e18),
        (("P",), ("peta",), 1e15),
        (("T",), ("tera",), 1e12),
        (("G",), ("giga",), 1e9),
        (("M",), ("mega",), 1e6),
        (("k",), ("kilo",), 1e3),
        (("h",), ("hecto",), 1e2),
        (("da",), ("deka", "deca"), 1e1),
        (("d",), ("deci",), 1e-1),
        (("c",), ("centi",), 1e-2),
        (("m",), ("milli",), 1e-3),
        (("u", "\N{MICRO SIGN}", "\N{GREEK SMALL LETTER MU}"), ("micro",), 1e-6),
        (("n",), ("nano",), 1e-9),
        (("p",), ("pico",), 1e-12),
        (("f",), ("femto",), 1e-15),
        (("a",), ("atto",), 1e-18),
        (("z",), ("zepto",), 1e-21),
        (("y",), ("yocto",), 1e-24),
        (("r",), ("ronto",), 1e-27),
        (("q",), ("quecto",), 1e-30),
    )
)


binary_prefixes: Final = tuple(
    UnitPrefix(symbols, names, factor)
    for symbols, names, factor in (
        (("Ki",), ("kibi",), 2**10),
        (("Mi",), ("mebi",), 2**20),
        (("Gi",), ("gibi",), 2**30),
        (("Ti",), ("tebi",), 2**40),
        (("Pi",), ("pebi",), 2**50),
        (("Ei",), ("exbi",), 2**60),
        (("Zi",), ("zebi",), 2**70),
        (("Yi",), ("yobi",), 2**80),
    )
)


def _add_prefixes(
    u: NamedUnit,
    excludes: Collection[str] = (),
    namespace: MutableMapping[str, object] | None = None,
    prefixes: bool | Iterable[UnitPrefix] = False,
) -> None:
    """
    Set up all of the standard metric prefixes for a unit.  This
    function should not be used directly, but instead use the
    `prefixes` kwarg on `def_unit` See the documentation of that function
    for the description of the parameters.
    """
    if prefixes is True:
        prefixes = si_prefixes
    elif prefixes is False:
        prefixes = []

    for short, full, factor in prefixes:
        names = []
        format = {}
        for prefix in short:
            if prefix in excludes:
                continue

            for alias in u.short_names:
                names.append(prefix + alias)

                # This is a hack to use Greek mu as a prefix
                # for some formatters.
                if prefix == "u":
                    format["latex"] = r"\mu " + u._get_format_name("latex")
                    format["unicode"] = "\N{MICRO SIGN}" + u._get_format_name("unicode")

                for key, val in u._format.items():
                    format.setdefault(key, prefix + val)

        for prefix in full:
            if prefix in excludes:
                continue

            for alias in u.long_names:
                names.append(prefix + alias)

        if names:
            PrefixUnit(
                names,
                CompositeUnit(factor, [u], [1], _error_check=False),
                namespace=namespace,
                format=format,
            )


@overload
def def_unit(
    s: str | list[str],
    represents: UnitLike,
    doc: str | None = None,
    format: Mapping[str, str] | None = None,
    prefixes: bool | Iterable[UnitPrefix] = False,
    exclude_prefixes: Collection[str] = (),
    namespace: MutableMapping[str, object] | None = None,
) -> Unit: ...
@overload
def def_unit(
    s: str | list[str],
    represents: None = None,
    doc: str | None = None,
    format: Mapping[str, str] | None = None,
    prefixes: bool | Iterable[UnitPrefix] = False,
    exclude_prefixes: Collection[str] = (),
    namespace: MutableMapping[str, object] | None = None,
) -> IrreducibleUnit: ...
def def_unit(
    s: str | list[str],
    represents: UnitLike | None = None,
    doc: str | None = None,
    format: Mapping[str, str] | None = None,
    prefixes: bool | Iterable[UnitPrefix] = False,
    exclude_prefixes: Collection[str] = (),
    namespace: MutableMapping[str, object] | None = None,
) -> NamedUnit:
    """Define a new unit.

    This function differs from creating units directly with `Unit` or
    `IrreducibleUnit` because it can also automatically generate prefixed
    units in the given namespace.

    Parameters
    ----------
    s : str or list of str
        The name of the unit.  If a list, the first element is the
        canonical (short) name, and the rest of the elements are
        aliases.

    represents : unit-like, optional
        The unit that this named unit represents.  If not provided,
        a new `IrreducibleUnit` is created.

    doc : str, optional
        A docstring describing the unit.

    format : dict, optional
        A mapping to format-specific representations of this unit.
        For example, for the ``Ohm`` unit, it might be nice to
        have it displayed as ``\\Omega`` by the ``latex``
        formatter.  In that case, `format` argument should be set
        to::

            {'latex': r'\\Omega'}

    prefixes : bool or iterable of UnitPrefix, optional
        When `True`, generate all of the SI prefixed versions of the
        unit as well.  For example, for a given unit ``m``, will
        generate ``mm``, ``cm``, ``km``, etc.  If only a few prefixed
        versions should be created then an iterable of `UnitPrefix`
        instances can be specified instead. Default is `False`, which
        means no prefixed versions will be generated.

        This function always returns the base unit object, even if
        multiple scaled versions of the unit were created.

    exclude_prefixes : `~collections.abc.Collection` of str, optional
        If any of the SI prefixes need to be excluded, they may be
        listed here.  For example, when defining the prefixes for ``a``,
        ``exclude_prefixes`` should be set to ``["P"]`` so that ``Pa``
        would still refer to the pascal.

        If a bare `str` is used then the prefixes that will be excluded are
        the substrings of the `str`, not just its individual characters.

    namespace : dict, optional
        When provided, inject the unit (and all of its aliases and
        prefixes), into the given namespace dictionary.

    Returns
    -------
    unit : `~astropy.units.NamedUnit`
        The newly-defined unit, or a matching unit that was already
        defined.

    Raises
    ------
    ValueError
        If ``represents`` cannot be parsed as a unit, e.g., because it is
        a malformed string or a |Quantity| that is not a scalar.

    """
    if represents is not None:
        result = Unit(s, represents, namespace=namespace, doc=doc, format=format)
    else:
        result = IrreducibleUnit(s, namespace=namespace, doc=doc, format=format)

    if prefixes:
        _add_prefixes(
            result, excludes=exclude_prefixes, namespace=namespace, prefixes=prefixes
        )
    return result


KNOWN_GOOD = np.ndarray | float | int | complex


def _condition_arg(value):
    """Validate value is acceptable for conversion purposes.

    Will convert into an array if not a scalar or array-like, where scalars
    and arrays can be python and numpy types, anything that defines
    ``__array_namespace__`` or anything that has a ``.dtype`` attribute.

    Parameters
    ----------
    value : scalar or array-like

    Returns
    -------
    Scalar value or array

    Raises
    ------
    ValueError
        If value is not as expected

    """
    if (
        isinstance(value, KNOWN_GOOD)
        or hasattr(value, "dtype")
        or hasattr(value, "__array_namespace__")
    ):
        return value

    value = np.array(value)
    if value.dtype.kind not in "ifc":
        raise ValueError(
            "Value not scalar compatible or convertible to "
            "an int, float, or complex array"
        )
    return value


def unit_scale_converter(val):
    """Function that just multiplies the value by unity.

    This is a separate function so it can be recognized and
    discarded in unit conversion.
    """
    return 1.0 * _condition_arg(val)


dimensionless_unscaled: Final[CompositeUnit] = CompositeUnit(
    1, [], [], _error_check=False
)
# Abbreviation of the above, see #1980
one: Final[CompositeUnit] = dimensionless_unscaled
