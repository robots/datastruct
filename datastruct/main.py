#  Copyright (c) Kuba Szczodrzyński 2023-1-3.

import dataclasses
import struct
from dataclasses import Field, dataclass
from functools import lru_cache
from typing import IO, Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

from .types import Config, Context, FieldMeta, FieldType, T
from .utils.const import ARRAYS, EXCEPTIONS
from .utils.context import build_context, evaluate
from .utils.fields import (
    field_decode,
    field_do_seek,
    field_encode,
    field_get_base,
    field_get_default,
    field_get_meta,
    field_get_padding,
    field_switch_base,
    field_validate,
)
from .utils.fmt import fmt_evaluate
from .utils.public import datastruct_get_config


@dataclass
class DataStruct:
    def __post_init__(self) -> None:
        for field, meta, value in self.fields():
            field_validate(field, meta)

            # accept special fields and those already having a value
            if value != Ellipsis or not meta.public:
                continue

            default = field_get_default(field, meta, DataStruct)
            if default is not None:
                # print("Got default for", field.name, default)
                self.__setattr__(field.name, default)
                continue

            # forbid creating an instance of fields with no default
            raise ValueError(
                f"Cannot create an instance of {type(self)}: "
                f"field '{field.name}' has no default and "
                f"no value was passed, nor can it be built",
            )

    def _write_value(self, ctx: Context, meta: FieldMeta, value: Any) -> Any:
        # build fields if necessary
        if meta.builder and (value is Ellipsis or meta.always):
            value = evaluate(ctx, meta.builder)
        # pack structures directly
        if isinstance(value, DataStruct):
            value.pack(io=ctx.io, parent=ctx)
            return value
        # evaluate and validate the format
        fmt = fmt_evaluate(ctx, meta.fmt, self.config().endianness)
        if isinstance(fmt, int):
            if len(value) < fmt:
                raise ValueError(f"Not enough bytes to write: {len(value)} < {fmt}")
            # assume the field is bytes, write it directly
            ctx.io.write(value[:fmt])
            return value
        # use struct.pack() to write the raw value
        encoded = field_encode(value)
        ctx.io.write(struct.pack(fmt, encoded))
        return value

    def _write_field(
        self,
        ctx: Context,
        field: Field,
        meta: FieldMeta,
        value: Any,
    ) -> None:
        if meta.ftype == FieldType.FIELD:
            value = self._write_value(ctx, meta, value)
            # update built value in the actual object
            setattr(self, field.name, value)
            setattr(ctx, field.name, value)
            return

        if meta.ftype == FieldType.SEEK:
            field_do_seek(ctx, meta)
            return

        if meta.ftype == FieldType.PADDING:
            _, padding, _ = field_get_padding(self.config(), ctx, meta)
            ctx.io.write(padding)
            return

        if meta.ftype == FieldType.REPEAT:
            # repeat() field - value type must be List
            if not isinstance(value, ARRAYS):
                raise TypeError(f"Value is not an array: {value}")
            items: Union[list, tuple] = value

            i = 0
            count = evaluate(ctx, meta.count)
            base_field, base_meta = field_get_base(meta)
            items_iter = iter(items)

            if isinstance(count, int) and len(value) != count and not base_meta.builder:
                # ensure the list size matches expected element count,
                # apart from built() subfields, which are implicitly trusted here
                raise ValueError(
                    f"List size ({len(value)}) doesn't match "
                    f"repeat() 'count' parameter value ({count})",
                )

            while count is None or i < count:
                ctx.i = i
                if meta.when and not evaluate(ctx, meta.when):
                    break

                if not base_meta.builder:
                    item = next(items_iter)
                else:
                    item = Ellipsis
                item = self._write_value(ctx, base_meta, item)
                if isinstance(items, list):
                    # don't reassign built fields to tuples
                    # only update in lists (which will update the object too)
                    if len(items) <= i:
                        items.append(item)
                    else:
                        items[i] = item

                # provide another value 'item' to context lambdas in 'last'
                ctx.item = item
                last = evaluate(ctx, meta.last)
                ctx.pop("item")
                if last:
                    break
                i += 1
            ctx.pop("i", None)
            return

        if meta.ftype == FieldType.COND:
            if evaluate(ctx, meta.condition) is False:
                return
            self._write_field(ctx, *field_get_base(meta), value)
            return

        if meta.ftype == FieldType.SWITCH:
            field = field_switch_base(self.config(), ctx, meta)
            meta = field_get_meta(field)
            if value is Ellipsis:
                # assign default based on field mode
                value = field_get_default(field, meta, DataStruct)
            self._write_field(ctx, field, meta, value)
            return

    @classmethod
    def _read_value(cls, ctx: Context, meta: FieldMeta, typ: Type[T]) -> T:
        # unpack structures directly
        if issubclass(typ, DataStruct):
            return typ.unpack(io=ctx.io, parent=ctx)
        # evaluate and validate the format
        fmt = fmt_evaluate(ctx, meta.fmt, cls.config().endianness)
        if isinstance(fmt, int):
            # assume the field is bytes, write it directly
            value = ctx.io.read(fmt)
            if len(value) < fmt:
                raise ValueError(f"Not enough bytes to read: {len(value)} < {fmt}")
            return value
        # use struct.unpack() to read the raw value
        length = struct.calcsize(fmt)
        (value,) = struct.unpack(fmt, ctx.io.read(length))
        value = field_decode(value, type)
        return value

    @classmethod
    def _read_field(
        cls,
        ctx: Context,
        field: Field,
        meta: FieldMeta,
    ) -> Any:
        if meta.ftype == FieldType.FIELD:
            value = cls._read_value(ctx, meta, field.type)
            ctx[field.name] = value
            return value

        if meta.ftype == FieldType.SEEK:
            field_do_seek(ctx, meta)
            return Ellipsis

        if meta.ftype == FieldType.PADDING:
            length, padding, check = field_get_padding(cls.config(), ctx, meta)
            if ctx.io.read(length) != padding and check:
                raise ValueError(f"Invalid padding found")
            return Ellipsis

        if meta.ftype == FieldType.REPEAT:
            # repeat() field - value type must be List
            if not issubclass(field.type, ARRAYS):
                raise TypeError("Field type is not an array")

            i = 0
            count = evaluate(ctx, meta.count)
            base_field, base_meta = field_get_base(meta)
            items = []
            ctx[field.name] = items

            while count is None or i < count:
                ctx.i = i
                if meta.when and not evaluate(ctx, meta.when):
                    break

                item = cls._read_value(ctx, base_meta, base_field.type)
                items.append(item)

                # provide another value 'item' to context lambdas in 'last'
                ctx.item = item
                last = evaluate(ctx, meta.last)
                ctx.pop("item")
                if last:
                    break
                i += 1
            ctx.pop("i", None)
            return items

        if meta.ftype == FieldType.COND:
            if evaluate(ctx, meta.condition) is False:
                if meta.if_not is not Ellipsis:
                    value = evaluate(ctx, meta.if_not)
                    ctx[field.name] = value
                    return value
                return Ellipsis
            return cls._read_field(ctx, *field_get_base(meta))

        if meta.ftype == FieldType.SWITCH:
            field = field_switch_base(cls.config(), ctx, meta)
            meta = field_get_meta(field)
            return cls._read_field(ctx, field, meta)

    def pack(
        self,
        io: IO[bytes],
        parent: Optional["Context"] = None,
    ) -> None:
        fields = self.fields()
        values = {f.name: v for f, m, v in fields if v != Ellipsis}
        ctx = build_context(parent, io, packing=True, unpacking=False, **values)
        field_name = type(self).__name__
        try:
            for field, meta, value in fields:
                field_name = f"{type(self).__name__}.{field.name}"
                # print(f"Packing {meta.ftype.name} '{field_name}'")
                self._write_field(ctx, field, meta, value)
        except EXCEPTIONS as e:
            suffix = f"; while packing '{field_name}'"
            e.args = (e.args[0] + suffix,)
            raise e

    @classmethod
    def unpack(
        cls: Type["DS"],
        io: IO[bytes],
        parent: Optional[Context] = None,
    ) -> "DS":
        fields = cls.classfields()
        values = {}
        ctx = build_context(parent, io, packing=False, unpacking=True)
        field_name = cls.__name__
        try:
            for field, meta in fields:
                field_name = f"{cls.__name__}.{field.name}"
                # print(f"Unpacking {meta.ftype.name} '{field_name}'")
                # validate fields since they weren't validated before
                field_validate(field, meta)
                value = cls._read_field(ctx, field, meta)
                if value is not Ellipsis:
                    values[field.name] = value
            field_name = f"{cls.__name__}()"
            # noinspection PyArgumentList
            return cls(**values)
        except EXCEPTIONS as e:
            suffix = f"; while unpacking '{field_name}'"
            e.args = (e.args[0] + suffix,)
            raise e

    def fields(self) -> List[Tuple[Field, FieldMeta, Any]]:
        return [
            (
                field,
                field_get_meta(field),
                self.__getattribute__(field.name),
            )
            for field in dataclasses.fields(self)
        ]

    @classmethod
    def classfields(cls) -> List[Tuple[Field, FieldMeta]]:
        return [
            (
                field,
                field_get_meta(field),
            )
            for field in dataclasses.fields(cls)
        ]

    def asdict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    @lru_cache
    def config(cls) -> Config:
        config = Config(datastruct_get_config())
        config.update(getattr(cls, "_CONFIG", {}))
        return config


DS = TypeVar("DS", bound=DataStruct)
