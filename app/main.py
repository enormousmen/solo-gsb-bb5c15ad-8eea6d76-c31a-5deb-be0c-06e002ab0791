"""FastAPI 入口：文档创建 / 读取 / 修订历史 / 带三方合并的更新。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import db
from .config import settings
from .merge import apply_merge_patch, present_conflict, three_way_merge
from .schemas import CreateDocument, CreateRelease, SetContract, UpdateDocument
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
    "基线过期时递归三方合并，路径冲突返回 409。",
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


# ---------------------------------------------------------------- 单项更新（共享）


class _ItemError(Exception):
    """单文档“基线→三方合并→契约校验”失败；调用方必须已在写事务内（失败即回滚）。

    单文档 PATCH 直接把它翻译成 HTTP 错误；原子发布则收集为结构化失败项，
    保证两条路径复用同一份合并与校验逻辑。
    """

    def __init__(self, code: str, status_code: int, **extra: Any) -> None:
        self.code = code
        self.status_code = status_code
        self.extra = extra
        super().__init__(code)


def prepare_document_update(
    conn: sqlite3.Connection,
    doc_id: str,
    base_revision: int,
    patch: Any,
    request_id: str,
) -> dict[str, Any]:
    """写事务内执行单项更新的**只读合并与校验**，返回落库所需的候选结果。

    顺序与单文档 PATCH 完全一致：
    ① 基线必须存在（``unknown_base_revision``）；
    ② Merge Patch 应用到基线得到 theirs；基线过期时递归三方合并
      （路径分歧 → ``conflict``）；
    ③ 文档存在契约时校验最终候选（``schema_validation_failed``）。

    本函数不写入任何数据，也不登记幂等记录；调用方负责插入修订 / 回滚。
    """
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise _ItemError("not_found", status.HTTP_404_NOT_FOUND, document_id=doc_id)

    base_row = db.get_revision(conn, doc_id, base_revision)
    if base_row is None:
        raise _ItemError(
            "unknown_base_revision",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            document_id=doc_id,
            base_revision=base_revision,
            current_revision=doc["current_revision"],
            reason="base_revision 不存在",
        )

    current_revision = doc["current_revision"]
    current_content = json.loads(
        db.get_revision(conn, doc_id, current_revision)["content"]
    )
    base_content = json.loads(base_row["content"])

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
            raise _ItemError(
                "conflict",
                status.HTTP_409_CONFLICT,
                document_id=doc_id,
                request_id=request_id,
                base_revision=base_revision,
                current_revision=current_revision,
                conflicts=[present_conflict(c) for c in raw_conflicts],
            )

    # 第二步（Merge Patch 与三方重放均完成后）：若文档存在契约，
    # 校验最终候选内容。失败由调用方回滚——不写修订、不占用幂等 ID。
    contract_row = db.get_contract(conn, doc_id)
    if contract_row is not None:
        contract_schema = json.loads(contract_row["schema_json"])
        schema_errors = validate_instance(contract_schema, merged)
        if schema_errors:
            raise _ItemError(
                "schema_validation_failed",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                document_id=doc_id,
                request_id=request_id,
                base_revision=base_revision,
                current_revision=current_revision,
                errors=schema_errors,
            )

    changed = db.canonical(merged) != db.canonical(current_content)
    return {
        "doc": doc,
        "merged": merged,
        "replayed": replayed,
        "changed": changed,
        "current_revision": current_revision,
        "updated_at": db.utcnow() if changed else doc["updated_at"],
    }


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

    # 写事务内重读，消除“检查后写入”的并发窗口（BEGIN IMMEDIATE 取写锁）
    with db.transaction(conn):
        doc = db.get_document(conn, doc_id)
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

        try:
            result = prepare_document_update(
                conn,
                doc_id,
                body.base_revision,
                body.patch,
                body.request_id,
            )
        except _ItemError as exc:
            raise err(exc.status_code, exc.code, **exc.extra)

        merged = result["merged"]
        replayed = result["replayed"]
        changed = result["changed"]
        current_revision = result["current_revision"]
        now = db.utcnow()
        if changed:
            new_revision = current_revision + 1
            db.insert_revision(
                conn,
                doc_id,
                new_revision,
                merged,
                body.patch,
                body.base_revision,
                body.request_id,
                replayed,
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
            "replayed": replayed,
            "changed": changed,
            "idempotent": False,
            "content": merged,
            "created_at": now if changed else result["updated_at"],
        }
    )


# ---------------------------------------------------------------- 原子发布


def _release_failure(index: int, document_id: str, exc: _ItemError) -> dict[str, Any]:
    """把单项失败整理成按输入顺序汇总的结构化失败项。"""
    failure: dict[str, Any] = {
        "index": index,
        "document_id": document_id,
        "error": exc.code,
        "status_code": exc.status_code,
    }
    failure.update(exc.extra)
    return failure


@app.post("/releases", tags=["releases"])
def create_release(
    body: CreateRelease, conn: sqlite3.Connection = Depends(get_conn)
) -> JSONResponse:
    """原子发布：在同一 SQLite 写事务内为多个文档各做一次三方合并 + 契约校验。

    * 任一文档不存在 / 基线无效 / 合并冲突 / 契约校验失败
      → 422 release_failed，``failures`` 按输入顺序汇总，全部文档不产生修订，
      且 release_id 不被占用；
    * 全部通过 → 一次性写入各文档新修订，返回每项最终 revision/content；
    * release_id 对**规范化整批请求**持久化幂等：
      相同请求（含顺序与 author）重放原结果，不同请求 409 release_id_conflict。
    """
    # 规范化指纹：按输入顺序记录每项 (document_id, base_revision, patch, author)，
    # 紧凑且键排序的确定性 JSON；字段顺序/空白变化不影响语义。
    fingerprint = hashlib.sha256(
        db.canonical(
            [
                [item.document_id, item.base_revision, item.patch, item.author]
                for item in body.updates
            ]
        ).encode("utf-8")
    ).hexdigest()

    existing = db.get_release(conn, body.release_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise err(
                status.HTTP_409_CONFLICT,
                "release_id_conflict",
                release_id=body.release_id,
                reason="同一 release_id 已用于 updates 不同的发布请求",
            )
        replay = json.loads(existing["results"])
        replay["idempotent"] = True
        return JSONResponse(replay)

    # 单一写事务（BEGIN IMMEDIATE）：全部通过才提交，任一失败整体回滚。
    with db.transaction(conn):
        # 事务内重读幂等记录，消除与并发发布之间的竞态窗口。
        existing = db.get_release(conn, body.release_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "release_id_conflict",
                    release_id=body.release_id,
                    reason="同一 release_id 已用于 updates 不同的发布请求",
                )
            replay = json.loads(existing["results"])
            replay["idempotent"] = True
            return JSONResponse(replay)

        # 逐项复用单文档 PATCH 的合并与契约校验逻辑，收集**全部**失败项。
        failures: list[dict[str, Any]] = []
        prepared: list[tuple[int, Any, dict[str, Any]]] = []
        for index, item in enumerate(body.updates):
            try:
                prepared.append(
                    (
                        index,
                        item,
                        prepare_document_update(
                            conn,
                            item.document_id,
                            item.base_revision,
                            item.patch,
                            body.release_id,
                        ),
                    )
                )
            except _ItemError as exc:
                failures.append(_release_failure(index, item.document_id, exc))

        if failures:
            # 不捕获：db.transaction 回滚，修订与 release_id 均不落库。
            raise err(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "release_failed",
                release_id=body.release_id,
                reason="发布中存在失败项，全部文档未产生修订",
                failures=failures,
            )

        # 全部通过：按输入顺序一次性写入各文档新修订。
        # 发布内 document_id 已在模型层保证互不重复，故逐项 +1 互不影响。
        now = db.utcnow()
        results: list[dict[str, Any]] = []
        for index, item, result in prepared:
            current_revision = result["current_revision"]
            merged = result["merged"]
            changed = result["changed"]
            if changed:
                new_revision = current_revision + 1
                db.insert_revision(
                    conn,
                    item.document_id,
                    new_revision,
                    merged,
                    item.patch,
                    item.base_revision,
                    body.release_id,
                    result["replayed"],
                    item.author,
                    now,
                )
                created_at = now
            else:
                # 自动重放后结果与当前版相同：不生成新修订
                new_revision = current_revision
                created_at = result["updated_at"]
            results.append(
                {
                    "document_id": item.document_id,
                    "revision": new_revision,
                    "base_revision": item.base_revision,
                    "replayed": result["replayed"],
                    "changed": changed,
                    "author": item.author,
                    "content": merged,
                    "created_at": created_at,
                }
            )

        payload: dict[str, Any] = {
            "release_id": body.release_id,
            "idempotent": False,
            "results": results,
        }
        # 仅持久化成功发布；失败路径在上面已抛出并回滚，不占用 release_id。
        db.insert_release(conn, body.release_id, fingerprint, payload, now)

    return JSONResponse(payload)
