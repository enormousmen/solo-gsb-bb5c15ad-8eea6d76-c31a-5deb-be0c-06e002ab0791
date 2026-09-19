"""JSON Schema 2020-12 契约校验。

提供两类校验：

* :func:`check_schema` —— 校验 Schema 本身是否为合法的 JSON Schema 2020-12
  （元数据校验，含布尔 Schema ``true`` / ``false``），并确认其中所有
  ``$ref`` 都能在**无网络检索**的注册表内解析（远程/错误指针在设置时即被拒，
  避免契约落库后在 PATCH 路径上延迟报错）；
* :func:`validate_instance` —— 用已通过自检的 Schema 校验文档实例，
  返回**全部**错误，错误按实例路径（RFC 6901 JSON Pointer）稳定排序。

错误渲染不抛出异常，统一返回结构化字典列表，便于在 API 层按错误码返回。
"""
from __future__ import annotations

from typing import Any, Union

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

# JSON Schema 文档的合法根类型：对象，或布尔（true/false 布尔 Schema）
SchemaRoot = Union[dict[str, Any], bool]


def _pointer(parts: list[Union[str, int]]) -> str:
    """渲染 RFC 6901 JSON Pointer；根为 ``""``，数字为数组下标。"""
    out = []
    for p in parts:
        token = str(p)
        out.append(token.replace("~", "~0").replace("/", "~1"))
    return "/" + "/".join(out) if out else ""


def _render(error: ValidationError) -> dict[str, Any]:
    return {
        # 错误在**实例**中的位置（调用方最关心的路径）
        "path": _pointer(list(error.absolute_path)),
        # 触发错误的 Schema 关键字，如 type / required / maximum
        "validator": error.validator,
        "message": error.message,
        # 错误在 **Schema** 中的位置，便于定位是哪条约束失败
        "schema_path": _pointer(list(error.absolute_schema_path)),
    }


def _sorted_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """稳定排序：先实例路径，再 Schema 路径，最后消息（消除关键字内顺序抖动）。"""
    return sorted(errors, key=lambda e: (e["path"], e["schema_path"], e["message"]))


def _collect_refs(schema: dict[str, Any]) -> list[tuple[str, str]]:
    """收集 Schema 中所有 ``$ref`` 及其所在位置的 JSON Pointer。

    同时遍历普通应用子 Schema 与 ``$defs`` 等位置（不区分关键字所在词汇表）。
    """
    found: list[tuple[str, str]] = []

    def walk(node: Any, path: list[Union[str, int]]) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                found.append((_pointer(path + ["$ref"]), ref))
            for key, value in node.items():
                walk(value, path + [key])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, path + [index])

    walk(schema, [])
    return found


def _unresolvable_ref_errors(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """以无远程检索器的注册表解析全部 ``$ref``，返回解析失败项。

    默认 Registry 不挂任何 retriever：远程 URI 会立即抛 Unresolvable，
    因此该检查不发起任何网络请求，适配离线/内网部署。
    """
    refs = _collect_refs(schema)
    if not refs:
        return []
    base = schema.get("$id", "") if isinstance(schema.get("$id"), str) else ""
    resource = DRAFT202012.create_resource(schema)
    registry = Registry().with_resource(base, resource)
    resolver = registry.resolver(base_uri=base)

    errors: list[dict[str, Any]] = []
    for location, ref in refs:
        try:
            resolver.lookup(ref)
        except Unresolvable as exc:
            errors.append(
                {
                    # Schema 自检的错误“实例”就是 Schema 本身
                    "path": location,
                    "validator": "unresolvable_ref",
                    "message": f"$ref 无法解析（不支持远程检索）: {ref}",
                    "schema_path": location,
                }
            )
    return _sorted_errors(errors)


def check_schema(schema: Any) -> list[dict[str, Any]]:
    """校验 *schema* 本身是否符合 JSON Schema 2020-12 元 Schema。

    :returns: 错误列表；为空表示 Schema 合法、可作为契约保存。
    """
    if not isinstance(schema, (dict, bool)):
        return [
            {
                "path": "",
                "validator": "root_type",
                "message": "schema 必须是 JSON 对象或布尔值（JSON Schema 2020-12）",
                "schema_path": "",
            }
        ]
    validator = Draft202012Validator(Draft202012Validator.META_SCHEMA)
    errors = [_render(e) for e in validator.iter_errors(schema)]
    if isinstance(schema, dict):
        # 元校验已报错时 Schema 结构不可信，不再做 $ref 解析
        if not errors:
            errors = _unresolvable_ref_errors(schema)
    return _sorted_errors(errors)


def validate_instance(schema: SchemaRoot, instance: Any) -> list[dict[str, Any]]:
    """用合法契约 *schema* 校验文档 *instance*，返回按实例路径排序的全部错误。

    ``iter_errors`` 不采用「快速失败」策略，可一次收集到所有实例位置的错误。
    调用方须保证 *schema* 已通过 :func:`check_schema`。
    """
    validator = Draft202012Validator(schema)
    return _sorted_errors([_render(e) for e in validator.iter_errors(instance)])
