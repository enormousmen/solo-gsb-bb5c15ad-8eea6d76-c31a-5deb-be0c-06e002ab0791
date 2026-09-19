"""FastAPI 入口：文档创建 / 读取 / 修订历史 / 带三方合并的更新 / 原子发布。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Iterator, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .merge import apply_merge_patch, present_conflict, three_way_merge
from .schemas import CreateDocument, CreateRelease, ReleaseItem, SetContract, UpdateDocument
from .validation import check_schema, validate_instance


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    # 一键启动：确保目录存在并自动建表
    db.init_db()
    yield


app = FastAPI(
    title="共享 JSON 配置三方合并 API",
    version="1.1.0",
    description="基于 JSON Merge Patch (RFC 7386) 的共享配置存储；"
    "基线过期时递归三方合并，路径冲突返回 409；"
    "POST /releases 在同一事务内原子发布多篇文档。",
    lifespan=lifespan,
)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def err(status_code: int, code: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"error": code}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def row_content(row: sqlite3.Row) -> Any:
    return json.loads(row["content"])


# ---------------------------------------------------------------- 健康检查


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {"service": "json-config-merge", "status": "ok", "docs": "/docs"}


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- 创建


@app.post("/documents", status_code=status.HTTP_201_CREATED, tags=["documents"])
def create_document(
    body: CreateDocument, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    doc_id = body.id or hashlib.sha1(db.utcnow().encode()).hexdigest()[:16]
    now = db.utcnow()
    try:
        with db.transaction(conn):
            if db.get_document(conn, doc_id) is not None:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "document_exists",
                    document_id=doc_id,
                    reason="文档 ID 已存在",
                )
            db.insert_document(conn, doc_id, body.content, body.author, now)
    except HTTPException:
        raise
    return {
        "id": doc_id,
        "revision": 1,
        "content": body.content,
        "author": body.author,
        "created_at": now,
    }


# ---------------------------------------------------------------- 读取


@app.get("/documents", tags=["documents"])
def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, current_revision, created_at, updated_at FROM documents"
        " ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@app.get("/documents/{doc_id}", tags=["documents"])
def get_document(
    doc_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    rev = db.get_revision(conn, doc_id, doc["current_revision"])
    return {
        "id": doc_id,
        "revision": doc["current_revision"],
        "content": row_content(rev),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


@app.get("/documents/{doc_id}/revisions", tags=["documents"])
def get_revisions(
    doc_id: str,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    limit = min(limit, settings.history_max_limit)
    rows = db.list_revisions(conn, doc_id, limit, offset, desc=(order == "desc"))
    items = [dict(r) for r in rows]
    for item in items:
        item["replayed"] = bool(item["replayed"])
    return {"document_id": doc_id, "items": items, "limit": limit, "offset": offset}


@app.get("/documents/{doc_id}/revisions/{revision}", tags=["documents"])
def get_one_revision(
    doc_id: str,
    revision: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    row = db.get_revision(conn, doc_id, revision)
    if row is None:
        raise err(
            status.HTTP_404_NOT_FOUND,
            "revision_not_found",
            document_id=doc_id,
            revision=revision,
        )
    return {
        "document_id": doc_id,
        "revision": row["revision"],
        "content": json.loads(row["content"]),
        "patch": json.loads(row["patch"]) if row["patch"] is not None else None,
        "base_revision": row["base_revision"],
        "request_id": row["request_id"],
        "replayed": bool(row["replayed"]),
        "author": row["author"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------- 契约（JSON Schema 2020-12）


@app.put("/documents/{doc_id}/schema", tags=["contracts"])
def put_contract(
    doc_id: str,
    body: SetContract,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """设置 / 替换文档契约。

    顺序：① 元校验 Schema 本身；② 在同一写事务内校验**当前文档**实例。
    任一步不通过都返回 422 且**不替换**既有契约（无契约时也不创建）。
    """
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    schema = body.schema_
    schema_errors = check_schema(schema)
    if schema_errors:
        raise err(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_schema",
            document_id=doc_id,
            reason="schema 本身不符合 JSON Schema 2020-12",
            errors=schema_errors,
        )

    with db.transaction(conn):
        # 事务内重读当前文档与既有契约：验证与替换原子完成，
        # 不与并发 PATCH 产生“按旧内容验证、按新内容替换”的窗口。
        doc = db.get_document(conn, doc_id)
        current_content = json.loads(
            db.get_revision(conn, doc_id, doc["current_revision"])["content"]
        )
        instance_errors = validate_instance(schema, current_content)
        if instance_errors:
            raise err(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "schema_validation_failed",
                document_id=doc_id,
                revision=doc["current_revision"],
                reason="当前文档不满足所给 schema，契约未替换",
                errors=instance_errors,
            )
        now = db.utcnow()
        db.upsert_contract(conn, doc_id, schema, now)

    return {
        "document_id": doc_id,
        "schema": schema,
        "revision": doc["current_revision"],
        "updated_at": now,
    }


@app.get("/documents/{doc_id}/schema", tags=["contracts"])
def get_contract(
    doc_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    row = db.get_contract(conn, doc_id)
    if row is None:
        raise err(
            status.HTTP_404_NOT_FOUND,
            "schema_not_found",
            document_id=doc_id,
            reason="该文档未设置契约",
        )
    return {
        "document_id": doc_id,
        "schema": json.loads(row["schema_json"]),
        "updated_at": row["updated_at"],
    }


@app.delete("/documents/{doc_id}/schema", tags=["contracts"])
def delete_contract(
    doc_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """删除契约；删除后 PATCH 恢复为无契约的原更新行为。幂等。"""
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    with db.transaction(conn):
        existed = db.get_contract(conn, doc_id) is not None
        db.delete_contract(conn, doc_id)
    return {"document_id": doc_id, "deleted": existed}


# ---------------------------------------------------------------- 更新（三方合并）


# plan_update 失败码 → HTTP 状态码（单文档接口的既有行为）
_FAILURE_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "unknown_base_revision": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "conflict": status.HTTP_409_CONFLICT,
    "schema_validation_failed": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def plan_update(
    conn: sqlite3.Connection, doc_id: str, base_revision: int, patch: Any
) -> Tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """在已开启的写事务内计算单文档更新计划（只读，不写库）。

    供 PATCH 单文档接口与 POST /releases 批量接口共用，保证两条路径的
    合并与契约校验语义完全一致。

    :returns: ``(plan, None)`` 或 ``(None, failure)``。*plan* 含合并结果、
              是否重放、是否与当前版相同及当前修订号；*failure* 为结构化
              错误详情（error / document_id / conflicts / errors 等）。
    """
    doc = db.get_document(conn, doc_id)
    if doc is None:
        return None, {"error": "not_found", "document_id": doc_id}

    base_row = db.get_revision(conn, doc_id, base_revision)
    if base_row is None:
        return None, {
            "error": "unknown_base_revision",
            "document_id": doc_id,
            "base_revision": base_revision,
            "current_revision": doc["current_revision"],
            "reason": "base_revision 不存在",
        }

    base_content = json.loads(base_row["content"])
    current_revision = doc["current_revision"]
    current_content = json.loads(
        db.get_revision(conn, doc_id, current_revision)["content"]
    )

    # 第一步：把 Merge Patch 应用到基线上，得到“来稿想要的结果”
    theirs = apply_merge_patch(base_content, patch)

    if base_revision == current_revision:
        # 基线就是当前版：快路径，无需三方比较
        merged, replayed = theirs, False
    else:
        # 基线过期：递归比较 base / current / theirs
        merged, raw_conflicts = three_way_merge(base_content, current_content, theirs)
        replayed = True
        if raw_conflicts:
            return None, {
                "error": "conflict",
                "document_id": doc_id,
                "base_revision": base_revision,
                "current_revision": current_revision,
                "conflicts": [present_conflict(c) for c in raw_conflicts],
            }

    # 第二步（Merge Patch 与三方重放均完成后）：若文档存在契约，
    # 校验最终候选内容。
    contract_row = db.get_contract(conn, doc_id)
    if contract_row is not None:
        contract_schema = json.loads(contract_row["schema_json"])
        schema_errors = validate_instance(contract_schema, merged)
        if schema_errors:
            return None, {
                "error": "schema_validation_failed",
                "document_id": doc_id,
                "base_revision": base_revision,
                "current_revision": current_revision,
                "errors": schema_errors,
            }

    plan = {
        "doc": doc,
        "merged": merged,
        "replayed": replayed,
        "changed": db.canonical(merged) != db.canonical(current_content),
        "current_revision": current_revision,
    }
    return plan, None


@app.patch("/documents/{doc_id}", tags=["documents"])
def update_document(
    doc_id: str,
    body: UpdateDocument,
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    # 幂等指纹：request_id 相同且 (base_revision, patch) 相同 → 同一请求
    fingerprint = hashlib.sha256(
        f"{body.base_revision}\n{db.canonical(body.patch)}".encode("utf-8")
    ).hexdigest()

    existing = db.get_request(conn, doc_id, body.request_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise err(
                status.HTTP_409_CONFLICT,
                "request_id_conflict",
                document_id=doc_id,
                request_id=body.request_id,
                reason="同一 request_id 已用于 base_revision/patch 不同的请求",
            )
        rev_row = db.get_revision(conn, doc_id, existing["revision"])
        return JSONResponse(
            {
                "id": doc_id,
                "revision": existing["revision"],
                "base_revision": body.base_revision,
                "request_id": body.request_id,
                "replayed": None,
                "changed": bool(existing["changed"]),
                "idempotent": True,
                "content": json.loads(rev_row["content"]),
                "created_at": rev_row["created_at"],
            }
        )

    base_row = db.get_revision(conn, doc_id, body.base_revision)
    if base_row is None:
        raise err(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "unknown_base_revision",
            document_id=doc_id,
            base_revision=body.base_revision,
            current_revision=doc["current_revision"],
            reason="base_revision 不存在",
        )

    # 写事务内重读，消除“检查后写入”的并发窗口（BEGIN IMMEDIATE 取写锁）
    with db.transaction(conn):
        existing = db.get_request(conn, doc_id, body.request_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "request_id_conflict",
                    document_id=doc_id,
                    request_id=body.request_id,
                    reason="同一 request_id 已用于 base_revision/patch 不同的请求",
                )
            current_row = db.get_revision(conn, doc_id, existing["revision"])
            payload = {
                "id": doc_id,
                "revision": existing["revision"],
                "base_revision": body.base_revision,
                "request_id": body.request_id,
                "replayed": None,
                "changed": bool(existing["changed"]),
                "idempotent": True,
                "content": json.loads(current_row["content"]),
                "created_at": current_row["created_at"],
            }
            return JSONResponse(payload)

        plan, failure = plan_update(conn, doc_id, body.base_revision, body.patch)
        if failure is not None:
            # 冲突 / 契约校验失败：事务回滚——不写修订、不占用 request_id
            if failure["error"] in ("conflict", "schema_validation_failed"):
                failure["request_id"] = body.request_id
            code = failure.pop("error")
            raise err(_FAILURE_STATUS[code], code, **failure)

        changed = plan["changed"]
        current_revision = plan["current_revision"]
        now = db.utcnow()
        if changed:
            new_revision = current_revision + 1
            db.insert_revision(
                conn,
                doc_id,
                new_revision,
                plan["merged"],
                body.patch,
                body.base_revision,
                body.request_id,
                plan["replayed"],
                body.author,
                now,
            )
        else:
            # 自动重放后结果与当前版相同：不生成新修订
            new_revision = current_revision

        db.insert_request(
            conn, doc_id, body.request_id, fingerprint, new_revision, changed
        )

    return JSONResponse(
        {
            "id": doc_id,
            "revision": new_revision,
            "base_revision": body.base_revision,
            "request_id": body.request_id,
            "replayed": plan["replayed"],
            "changed": changed,
            "idempotent": False,
            "content": plan["merged"],
            "created_at": now if changed else plan["doc"]["updated_at"],
        }
    )


# ---------------------------------------------------------------- 原子发布（批量）


def _release_fingerprint(items: list[ReleaseItem]) -> str:
    """整批请求的规范化指纹：逐项 (document_id, base_revision, patch, author)
    按输入顺序做确定性 JSON 序列化后取 SHA-256。"""
    normalized = [
        {
            "document_id": item.document_id,
            "base_revision": item.base_revision,
            "patch": item.patch,
            "author": item.author,
        }
        for item in items
    ]
    return hashlib.sha256(db.canonical(normalized).encode("utf-8")).hexdigest()


def _release_replay(row: sqlite3.Row) -> JSONResponse:
    """幂等重放：返回首次成功时持久化的原始响应（仅 idempotent 标记不同）。"""
    payload = json.loads(row["response"])
    payload["idempotent"] = True
    return JSONResponse(payload)


@app.post("/releases", tags=["releases"])
def create_release(
    body: CreateRelease, conn: sqlite3.Connection = Depends(get_conn)
) -> JSONResponse:
    """原子发布：同一 SQLite 写事务内对多篇文档逐项执行三方合并 + 契约校验。

    全部通过才一次性写入各文档的新修订；任一文档失败则整批回滚，
    按输入顺序汇总返回结构化失败项，且 release_id 不被占用。
    """
    fingerprint = _release_fingerprint(body.items)

    existing = db.get_release(conn, body.release_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise err(
                status.HTTP_409_CONFLICT,
                "release_id_conflict",
                release_id=body.release_id,
                reason="同一 release_id 已用于内容不同的发布请求",
            )
        return _release_replay(existing)

    with db.transaction(conn):
        # 事务内复查，消除并发同 ID 发布的竞态
        existing = db.get_release(conn, body.release_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "release_id_conflict",
                    release_id=body.release_id,
                    reason="同一 release_id 已用于内容不同的发布请求",
                )
            return _release_replay(existing)

        # 逐项计划（只读）：复用单文档的三方合并与契约校验，汇总全部失败
        plans: list[Tuple[ReleaseItem, dict[str, Any]]] = []
        failures: list[dict[str, Any]] = []
        for index, item in enumerate(body.items):
            plan, failure = plan_update(
                conn, item.document_id, item.base_revision, item.patch
            )
            if failure is not None:
                failures.append({"index": index, **failure})
            else:
                plans.append((item, plan))

        if failures:
            # 整批回滚：任何文档都不产生修订，release_id 不被占用
            raise err(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "release_failed",
                release_id=body.release_id,
                reason="部分文档更新失败，整批已回滚",
                failures=failures,
            )

        # 全部通过：一次性写入各文档的新修订
        now = db.utcnow()
        items_out: list[dict[str, Any]] = []
        for item, plan in plans:
            changed = plan["changed"]
            if changed:
                new_revision = plan["current_revision"] + 1
                db.insert_revision(
                    conn,
                    item.document_id,
                    new_revision,
                    plan["merged"],
                    item.patch,
                    item.base_revision,
                    body.release_id,
                    plan["replayed"],
                    item.author,
                    now,
                )
            else:
                # 与当前版完全相同：该文档不生成新修订
                new_revision = plan["current_revision"]
            items_out.append(
                {
                    "document_id": item.document_id,
                    "revision": new_revision,
                    "base_revision": item.base_revision,
                    "replayed": plan["replayed"],
                    "changed": changed,
                    "content": plan["merged"],
                    "created_at": now if changed else plan["doc"]["updated_at"],
                }
            )

        response = {
            "release_id": body.release_id,
            "idempotent": False,
            "items": items_out,
            "created_at": now,
        }
        # 仅整批成功才持久化 release_id（失败不占用 ID）
        db.insert_release(
            conn, body.release_id, fingerprint, db.canonical(response), now
        )

    return JSONResponse(response)
