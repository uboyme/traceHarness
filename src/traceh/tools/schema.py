"""A deliberately small JSON Schema validator for tool arguments."""

from __future__ import annotations

from traceh.api.json_types import JsonValue


class ToolArgumentError(ValueError):
    pass


def _declared_shape(property_schema: dict) -> str:
    """Name the keys this argument's own schema requires.

    A caller that sent the wrong type has already been given the schema, but the
    rejection alone does not say which part was wrong. Repeating the declared
    field names turns a rejection into something correctable in one attempt
    instead of the same mistake retried; nothing here is new information.
    """

    required = property_schema.get("required")
    properties = property_schema.get("properties")
    names: list[str] = []
    if isinstance(required, list):
        names = [str(item) for item in required]
    elif isinstance(properties, dict):
        names = [str(key) for key in properties]
    if not names:
        return ""
    return " (an object with keys: " + ", ".join(names) + ")"


def _matches_type(value: JsonValue, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def validate_arguments(arguments: dict[str, JsonValue], schema: dict[str, JsonValue]) -> None:
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and expected_type != "object":
        raise ToolArgumentError("top-level tool schema must be an object")

    required = schema.get("required", [])
    if isinstance(required, list):
        for name in required:
            if isinstance(name, str) and name not in arguments:
                raise ToolArgumentError(f"missing required argument: {name}")

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return

    additional = schema.get("additionalProperties", True)
    if additional is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolArgumentError(f"unexpected arguments: {', '.join(unknown)}")

    for name, value in arguments.items():
        property_schema = properties.get(name)
        if not isinstance(property_schema, dict):
            continue
        expected = property_schema.get("type")
        expected_types: list[str]
        if isinstance(expected, str):
            expected_types = [expected]
        elif isinstance(expected, list):
            expected_types = [str(item) for item in expected]
        else:
            expected_types = []
        if expected_types and not any(_matches_type(value, item) for item in expected_types):
            raise ToolArgumentError(
                f"argument {name!r} must be {' or '.join(expected_types)}, "
                f"got {type(value).__name__}{_declared_shape(property_schema)}"
            )
        enum_values = property_schema.get("enum")
        if isinstance(enum_values, list) and value not in enum_values:
            raise ToolArgumentError(f"argument {name!r} must be one of {enum_values!r}")
        if isinstance(value, list):
            item_schema = property_schema.get("items")
            if isinstance(item_schema, dict):
                item_type = item_schema.get("type")
                if isinstance(item_type, str):
                    for index, item in enumerate(value):
                        if not _matches_type(item, item_type):
                            raise ToolArgumentError(
                                f"argument {name!r}[{index}] must be {item_type}"
                            )
