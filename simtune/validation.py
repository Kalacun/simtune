"""Dependency-free validation for extension records, separate from legacy storage.

Only format/consistency checks live here. No host discovery, workload execution,
policy resolution, scientific validation, or evidence selection is performed.
"""

from __future__ import annotations

import copy
import json
import math
import re
import types
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints


class ValidationError(ValueError):
    """Invalid extension data, with a field path suitable for user diagnostics."""


def require(condition: bool, path: str, message: str) -> None:
    if not condition:
        raise ValidationError(f"{path}: {message}")


def artifact_path(root: str | Path, reference: str) -> Path:
    """Resolve a relative artifact without escaping root, including via symlinks.

    This checks the filesystem at call time, not a sandbox against concurrent
    symlink replacement. Consumers must recheck immediately before file access.
    Missing leaves are allowed for planned output artifacts.
    """
    require(type(reference) is str, "artifact", "expected string reference")
    _constraint(reference, "artifact", "artifact", None)
    try:
        base = Path(root).resolve(strict=True)
        require(base.is_dir(), "root", "must be an existing directory")
        try:
            resolved = (base / reference).resolve(strict=True)
        except FileNotFoundError:
            resolved = (base / reference).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"artifact: cannot resolve {reference!r}: {exc}") from exc
    require(resolved.is_relative_to(base), "artifact", "path escapes record root")
    return resolved


def _json_value(value: Any, path: str) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        require(math.isfinite(value), path, "number must be finite")
    elif type(value) is list:
        for i, item in enumerate(value):
            _json_value(item, f"{path}[{i}]")
    elif type(value) is dict:
        for key, item in value.items():
            require(type(key) is str, path, "object keys must be strings")
            _json_value(item, f"{path}.{key}")
    else:
        raise ValidationError(f"{path}: expected JSON data, not {type(value).__name__}")


def _constraint(value: Any, rule: str, path: str, root: Path | None) -> None:
    if rule == "json":
        _json_value(value, path)
    elif rule == "text":
        require(bool(value.strip()) and "\0" not in value, path, "must be nonblank and NUL-free")
    elif rule == "arg":
        require("\0" not in value, path, "must be NUL-free")
    elif rule == "id":
        require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is not None,
            path,
            "ID must be 1–128 ASCII letters/digits/underscores/hyphens, starting alphanumeric",
        )
    elif rule == "sha256":
        require(re.fullmatch(r"[0-9a-f]{64}", value) is not None, path, "expected SHA-256 hex")
    elif rule == "positive":
        require(value > 0, path, "must be positive")
    elif rule == "nonnegative":
        require(value >= 0, path, "must be nonnegative")
    elif rule == "fraction":
        require(0 <= value < 1, path, "must be in [0, 1)")
    elif rule == "unique":
        require(len(set(value)) == len(value), path, "duplicate values are not allowed")
    elif rule == "absolute":
        require(Path(value).is_absolute() and "\0" not in value, path, "expected absolute path")
    elif rule == "artifact":
        require(
            bool(value)
            and not value.startswith("/")
            and not re.match(r"[A-Za-z]:", value)
            and "\\" not in value
            and not any(part in ("", ".", "..") for part in value.split("/"))
            and not any(ord(c) < 32 for c in value),
            path,
            "expected relative artifact path without empty, dot, parent, or backslash components",
        )
        if root is not None:
            artifact_path(root, value)
    elif rule == "timestamp":
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{path}: expected ISO 8601 timestamp with timezone") from exc
        require(stamp.tzinfo is not None, path, "timestamp requires timezone")
    else:
        raise TypeError(f"unknown validation rule {rule}")


@lru_cache(maxsize=None)
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls, include_extras=True)


def _typed(value: Any, annotation: Any, path: str, root: Path | None, decode: bool) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        result = _typed(value, args[0], path, root, decode)
        for rule in args[1:]:
            _constraint(result, rule, path, root)
        return result
    if origin in (Union, types.UnionType):
        nonnull = [option for option in args if option is not type(None)]
        if type(None) in args and len(nonnull) == 1:
            return None if value is None else _typed(value, nonnull[0], path, root, decode)
        for option in args:
            try:
                return _typed(value, option, path, root, decode)
            except ValidationError:
                pass
        raise ValidationError(f"{path}: value does not match {annotation}")
    if annotation is Any:
        _json_value(value, path)
        return copy.deepcopy(value) if decode else value
    if annotation is type(None):
        require(value is None, path, "expected null")
    elif origin is list:
        require(type(value) is list, path, "expected array")
        result = [
            _typed(item, args[0], f"{path}[{i}]", root, decode) for i, item in enumerate(value)
        ]
        ids = [item.id for item in result if hasattr(item, "id")]
        require(len(ids) == len(set(ids)), path, "duplicate IDs are not allowed")
        return result
    elif origin is dict:
        require(type(value) is dict, path, "expected object")
        return {
            _typed(k, args[0], path, root, decode): _typed(v, args[1], f"{path}.{k}", root, decode)
            for k, v in value.items()
        }
    elif isinstance(annotation, type) and issubclass(annotation, Enum):
        if decode:
            require(type(value) is str, path, "expected enum string")
            try:
                return annotation(value)
            except ValueError as exc:
                raise ValidationError(
                    f"{path}: expected one of {[x.value for x in annotation]}"
                ) from exc
        require(isinstance(value, annotation), path, f"expected {annotation.__name__}")
    elif is_dataclass(annotation):
        if decode:
            return decode_model(annotation, value, root=root, path=path)
        require(type(value) is annotation, path, f"expected {annotation.__name__}")
        validate_model(value, root=root, path=path)
    elif annotation is float:
        require(type(value) in (int, float), path, "expected finite number (boolean excluded)")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        require(finite, path, "number must be finite and representable as a float")
    else:
        require(type(value) is annotation, path, f"expected {annotation.__name__}")
    return value


def validate_model(model: Any, *, root: Path | None = None, path: str = "record") -> None:
    for item in fields(model):
        _typed(
            getattr(model, item.name),
            _hints(type(model))[item.name],
            f"{path}.{item.name}",
            root,
            False,
        )
    # Null is explicit and always explained, including not-applicable states.
    nullable = {f.name for f in fields(model) if getattr(model, f.name) is None}
    require(
        set(model.unavailable) == nullable,
        f"{path}.unavailable",
        f"must explain exactly null fields: {sorted(nullable)}",
    )
    model._validate(path)


def decode_model(cls: type, data: Any, *, root: Path | None = None, path: str = "record") -> Any:
    require(type(data) is dict, path, "expected object")
    names = {f.name for f in fields(cls)}
    kind = getattr(cls, "KIND", None)
    envelope = {"kind", "schema_version"} if kind else set()
    if kind:
        require(data.get("kind") == kind, f"{path}.kind", f"expected {kind!r}")
        require(
            type(data.get("schema_version")) is int and data["schema_version"] == 1,
            f"{path}.schema_version",
            "unsupported version; expected integer 1",
        )
    require(
        not (set(data) - names - envelope),
        path,
        f"unknown fields: {sorted(set(data) - names - envelope, key=str)}",
    )
    values = {}
    for item in fields(cls):
        if item.name not in data:
            require(
                item.default is not MISSING or item.default_factory is not MISSING,
                f"{path}.{item.name}",
                "missing required field",
            )
            continue
        values[item.name] = _typed(
            data[item.name], _hints(cls)[item.name], f"{path}.{item.name}", root, True
        )
    result = cls(**values)
    validate_model(result, root=root, path=path)
    return result


def encode_model(model: Any, *, root: Path | None = None) -> dict[str, Any]:
    validate_model(model, root=root)

    def encode(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value):
            result = {f.name: encode(getattr(value, f.name)) for f in fields(value)}
            if kind := getattr(value, "KIND", None):
                result.update(kind=kind, schema_version=1)
            return result
        if isinstance(value, list):
            return [encode(item) for item in value]
        if isinstance(value, dict):
            return {key: encode(item) for key, item in value.items()}
        return value

    return encode(model)


def restore_toml_nulls(annotation: Any, value: Any) -> Any:
    """Restore explained nulls only at typed model fields, never in opaque JSON."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return restore_toml_nulls(args[0], value)
    if origin in (Union, types.UnionType):
        options = [option for option in args if option is not type(None)]
        if value is not None and len(options) == 1:
            return restore_toml_nulls(options[0], value)
    if origin is list and isinstance(value, list):
        return [restore_toml_nulls(args[0], item) for item in value]
    if is_dataclass(annotation) and isinstance(value, dict):
        result = dict(value)
        hints = _hints(annotation)
        unavailable = value.get("unavailable", {})
        for item in fields(annotation):
            hint = hints[item.name]
            if (
                item.name not in result
                and isinstance(unavailable, dict)
                and item.name in unavailable
                and type(None) in get_args(hint)
            ):
                result[item.name] = None
            if item.name in result:
                result[item.name] = restore_toml_nulls(hint, result[item.name])
        return result
    return value


def load_json(text: str) -> Any:
    """Reject duplicate keys and JSON's non-standard NaN/Infinity spellings."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            require(key not in result, key, "duplicate JSON key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValidationError(f"JSON: nonfinite constant {value}")

    try:
        data = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"JSON line {exc.lineno}: {exc.msg}") from exc
    _json_value(data, "JSON")
    return data
