#  Copyright (c) Kuba Szczodrzyński 2023-1-6.

import dataclasses
from dataclasses import MISSING, Field, is_dataclass
from enum import Enum
from io import SEEK_CUR
from typing import Any, Callable, Optional, Tuple

from ..types import Context, FieldMeta, FieldType
from .const import ARRAYS
from .context import evaluate
from .fmt import fmt_check
from .misc import pad_up, repstr


def build_field(
    ftype: FieldType,
    default: Any = ...,
    default_factory: Any = MISSING,
    *,
    public: bool = True,
    **kwargs,
) -> Field:
    if isinstance(default, (list, dict, set)) or is_dataclass(default):
        raise ValueError(
            f"Mutable default {type(default)} is not allowed: use default_factory",
        )
    if default_factory is not MISSING:
        default = MISSING
    # noinspection PyArgumentList
    return dataclasses.field(
        init=public,
        repr=public,
        compare=public,
        default=default,
        default_factory=default_factory,
        metadata=dict(
            datastruct=FieldMeta(
                validated=False,
                public=public,
                ftype=ftype,
                **kwargs,
            ),
        ),
    )


def build_wrapper(
    ftype: FieldType,
    default: Any = None,
    default_factory: Any = None,
    **kwargs,
) -> Callable[[Field], Field]:
    def wrap(base: Field):
        return build_field(
            ftype=ftype,
            default=default or base.default,
            default_factory=default_factory or base.default_factory,
            public=base.init,
            # meta
            base=base,
            **kwargs,
        )

    return wrap


def field_encode(v: Any) -> Any:
    if isinstance(v, str):
        return v.encode()
    if isinstance(v, int):
        return v
    if isinstance(v, Enum):
        return v.value
    return v


def field_decode(v: Any, cls: type) -> Any:
    if issubclass(cls, str):
        return v.decode()
    if issubclass(cls, Enum):
        return cls(v)
    return v


def field_get_type(field: Field) -> Tuple[type, Optional[type]]:
    ftype = field.type
    if ftype is Ellipsis:
        return ftype, None
    if hasattr(ftype, "__origin__"):
        ftype = field.type.__origin__
    if issubclass(ftype, ARRAYS) and hasattr(field.type, "__args__"):
        return ftype, field.type.__args__[0]
    return ftype, None


def field_get_meta(field: Field) -> FieldMeta:
    if not field.metadata:
        raise ValueError(
            f"Can't find field metadata of '{field.name}'; "
            f"use datastruct.field() instead of dataclass.field(); "
            f"remember to invoke wrapper fields (i.e. repeat()(), cond()()) "
            f"passing the base field in the parameters",
        )
    return field.metadata["datastruct"]


def field_get_base(meta: FieldMeta) -> Tuple[Field, FieldMeta]:
    return meta.base, field_get_meta(meta.base)


def field_do_seek(ctx: Context, meta: FieldMeta) -> None:
    offset = evaluate(ctx, meta.offset)
    if meta.whence == SEEK_CUR or meta.absolute:
        ctx.absseek(offset, meta.whence)
    else:
        ctx.seek(offset, meta.whence)


def field_get_padding(ctx: Context, meta: FieldMeta) -> Tuple[int, bytes]:
    if meta.length:
        length = evaluate(ctx, meta.length)
    elif meta.modulus:
        modulus = evaluate(ctx, meta.modulus)
        tell = ctx.abstell() if meta.absolute else ctx.tell()
        length = pad_up(tell, modulus)
    else:
        raise ValueError("Unknown padding type")
    return length, repstr(meta.pattern, length)


def field_validate(field: Field, meta: FieldMeta) -> None:
    if meta.validated:
        return
    field_type, item_type = field_get_type(field)

    # skip special fields (seek, padding, etc)
    if not meta.public:
        if field_type is not Ellipsis:
            raise TypeError("Use Ellipsis (...) for special fields")
        return
    if field_type is Ellipsis:
        raise TypeError("Cannot use Ellipsis for standard fields")

    # check some known type constraints
    if meta.ftype == FieldType.FIELD:
        if item_type is not None:
            # var: List[...] = field(...)
            raise ValueError("Can't use a list without repeat() wrapper")
        if is_dataclass(field_type) and meta.fmt:
            # var: DataStruct = field(...)
            raise ValueError("Use subfield() for instances of DataStruct")
        if meta.fmt:
            # validate format specifiers
            fmt_check(meta.fmt)

    elif meta.ftype == FieldType.REPEAT:
        base_field, base_meta = field_get_base(meta)
        if item_type is None:
            # var: ... = repeat()(...)
            raise ValueError("Can't use repeat() for a non-list field")
        if base_meta.ftype != FieldType.FIELD:
            # var: ... = repeat()(padding(...))
            raise ValueError(
                "Only field(), subfield() and built() can be used with repeat()"
            )
        if base_meta.builder and not base_meta.always:
            # var: ... = repeat()(built(..., always=False))
            raise ValueError("Built fields inside repeat() are always built")

    # update types and validate base fields
    if meta.base:
        base_field, base_meta = field_get_base(meta)
        base_field.name = field.name
        base_field.type = field.type
        if meta.ftype == FieldType.REPEAT:
            # "unwrap" item types for repeat fields only
            field.type = field_type
            base_field.type = item_type or field_type
        field_validate(base_field, base_meta)
    meta.validated = True