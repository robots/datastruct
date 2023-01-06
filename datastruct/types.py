#  Copyright (c) Kuba Szczodrzyński 2023-1-3.

from dataclasses import Field
from enum import Enum, auto
from typing import IO, Any, Callable, TypeVar, Union


class Container(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__dict__ = self

    def __getattribute__(self, name: str):
        try:
            return super().__getattribute__(name)
        except AttributeError:
            return None


class Context(Container):
    _: Any
    io: IO[bytes]
    packing: bool
    unpacking: bool
    i: int
    item: Any
    tell: Callable[[], int]
    seek: Union[Callable[[int], int], Callable[[int, int], int]]
    skip: Callable[[int], int]
    abstell: Callable[[], int]
    absseek: Union[Callable[[int], int], Callable[[int, int], int]]


T = TypeVar("T")

V = TypeVar("V")
Eval = Callable[[Context], V]
Value = Union[Eval[V], V]
FormatType = Value[Union[str, int]]


class FieldType(Enum):
    # standard field
    FIELD = auto()  # field(), subfield(), built()
    # special fields
    SEEK = auto()  # seek(), skip()
    PADDING = auto()  # padding(), align()
    # wrapper fields
    REPEAT = auto()  # repeat()
    COND = auto()  # cond()


class FieldMeta(Container):
    validated: bool
    public: bool
    ftype: FieldType
    # FIELD
    fmt: FormatType
    builder: Value[Any]
    always: bool
    # SEEK
    offset: Value[int]
    whence: int
    absolute: bool
    # PADDING
    length: Value[int]
    modulus: Value[int]
    pattern: bytes
    check: bool
    # REPEAT
    base: Field
    count: Value[int]
    when: Eval[bool]
    last: Eval[bool]
    # COND
    condition: Value[bool]
    if_not: Value[Any]


class Endianness(Enum):
    DEFAULT = "@"
    NATIVE = "="
    LITTLE = "<"
    BIG = ">"
    NETWORK = "!"


DEFAULT = Endianness.DEFAULT
NATIVE = Endianness.NATIVE
LITTLE = Endianness.LITTLE
BIG = Endianness.BIG
NETWORK = Endianness.NETWORK


class Config(Container):
    endianness: Endianness
    padding_pattern: bytes
    padding_check: bool
