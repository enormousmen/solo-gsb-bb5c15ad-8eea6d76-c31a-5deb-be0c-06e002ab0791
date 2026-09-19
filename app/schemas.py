"""请求/响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# JSON 允许的值类型
_JSON_TYPES = (dict, list, str, int, float, bool, type(None))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDocument(StrictModel):
    id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$",
        description="文档 ID；省略时服务端生成。允许字母数字及 . _ ~ -",
    )
    content: Any = Field(default_factory=dict, description="初始 JSON 配置")
    author: Optional[str] = Field(default=None, max_length=200)

    @field_validator("content")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, _JSON_TYPES):
            raise ValueError("content 必须是合法的 JSON 值")
        return v


class UpdateDocument(StrictModel):
    base_revision: int = Field(
        ..., ge=1, description="来稿所基于的修订号（文档读取时返回）"
    )
    request_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="调用方生成的去重 ID（如 UUID），同 ID+同内容幂等",
    )
    patch: Any = Field(..., description="RFC 7386 JSON Merge Patch")
    author: Optional[str] = Field(default=None, max_length=200)

    @field_validator("patch")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, _JSON_TYPES):
            raise ValueError("patch 必须是合法的 JSON 值")
        return v


class ReleaseItem(StrictModel):
    """原子发布中的单篇文档更新（语义与 PATCH /documents/{id} 一致）。"""

    document_id: str = Field(..., min_length=1, max_length=128)
    base_revision: int = Field(
        ..., ge=1, description="来稿所基于的修订号（文档读取时返回）"
    )
    patch: Any = Field(..., description="RFC 7386 JSON Merge Patch")
    author: Optional[str] = Field(default=None, max_length=200)

    @field_validator("patch")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, _JSON_TYPES):
            raise ValueError("patch 必须是合法的 JSON 值")
        return v


class CreateRelease(StrictModel):
    release_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="调用方生成的整批去重 ID（如 UUID），同 ID+同内容幂等",
    )
    items: list[ReleaseItem] = Field(
        ...,
        min_length=2,
        max_length=20,
        description="2～20 项文档更新，document_id 互不重复，按输入顺序处理",
    )

    @field_validator("items")
    @classmethod
    def _unique_document_ids(cls, v: list[ReleaseItem]) -> list[ReleaseItem]:
        seen: set[str] = set()
        for item in v:
            if item.document_id in seen:
                raise ValueError(f"items 中 document_id 重复: {item.document_id}")
            seen.add(item.document_id)
        return v


class SetContract(StrictModel):
    # JSON Schema 根必须是对象或布尔（true/false 布尔 Schema）；
    # 进一步的合法性（关键字、$ref 等）由 validation.check_schema 做元校验。
    schema_: Any = Field(
        ...,
        alias="schema",
        description="JSON Schema 2020-12 契约（对象或布尔）",
    )

    @field_validator("schema_")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, (dict, bool)):
            raise ValueError("schema 必须是 JSON 对象或布尔值（JSON Schema 2020-12）")
        return v
