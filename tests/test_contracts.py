"""契约（JSON Schema 2020-12）端到端集成测试（每个用例使用独立临时数据库）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


SCHEMA = {
    "type": "object",
    "properties": {
        "a": {"type": "integer", "minimum": 0},
        "b": {"type": "string", "minLength": 1},
    },
    "required": ["a", "b"],
    "additionalProperties": False,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _create(client, content, doc_id="cfg"):
    r = client.post("/documents", json={"id": doc_id, "content": content})
    assert r.status_code == 201, r.text
    return r.json()


def _set_schema(client, doc_id, schema):
    return client.put(f"/documents/{doc_id}/schema", json={"schema": schema})


def _patch(client, doc_id, base_revision, request_id, patch):
    return client.patch(
        f"/documents/{doc_id}",
        json={"base_revision": base_revision, "request_id": request_id, "patch": patch},
    )


# ---------------- 设置 / 查询 / 删除 ----------------


def test_unresolvable_refs_rejected_at_set_time(client):
    _create(client, {"a": 1})
    # 远程 $ref（离线无法解析）→ 元校验通过但引用检查失败
    r = _set_schema(
        client,
        "cfg",
        {"properties": {"a": {"$ref": "https://example.com/missing.json"}}},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_schema"
    assert detail["errors"][0]["validator"] == "unresolvable_ref"
    assert detail["errors"][0]["path"] == "/properties/a/$ref"
    assert client.get("/documents/cfg/schema").status_code == 404
    # 错误的内部指针同样拒绝
    r = _set_schema(client, "cfg", {"properties": {"a": {"$ref": "#/$defs/nope"}}})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["validator"] == "unresolvable_ref"


def test_internal_defs_ref_schema_works(client):
    _create(client, {"a": 5})
    schema = {
        "$defs": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}},
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/port"}},
    }
    assert _set_schema(client, "cfg", schema).status_code == 200
    assert _patch(client, "cfg", 1, "ok", {"a": 8080}).status_code == 200
    r = _patch(client, "cfg", 2, "bad", {"a": 70000})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["path"] == "/a"


def test_set_get_delete_contract(client):
    _create(client, {"a": 1, "b": "x"})
    r = _set_schema(client, "cfg", SCHEMA)
    assert r.status_code == 200, r.text
    assert r.json()["schema"] == SCHEMA
    assert r.json()["revision"] == 1

    r = client.get("/documents/cfg/schema")
    assert r.status_code == 200
    assert r.json()["schema"] == SCHEMA
    assert "updated_at" in r.json()

    r = client.delete("/documents/cfg/schema")
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    assert client.get("/documents/cfg/schema").status_code == 404
    # 再删一次：幂等，deleted=false
    r = client.delete("/documents/cfg/schema")
    assert r.status_code == 200
    assert r.json()["deleted"] is False


def test_get_schema_on_document_without_contract_is_404(client):
    _create(client, {})
    r = client.get("/documents/cfg/schema")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "schema_not_found"


def test_contract_endpoints_missing_document_404(client):
    assert client.get("/documents/ghost/schema").status_code == 404
    assert client.delete("/documents/ghost/schema").status_code == 404
    assert _set_schema(client, "ghost", {}).status_code == 404


def test_set_contract_extra_field_rejected(client):
    _create(client, {})
    r = client.put(
        "/documents/cfg/schema", json={"schema": {}, "bogus": 1}
    )
    assert r.status_code == 422


def test_set_contract_wrong_root_type_rejected(client):
    _create(client, {})
    # 顶层为数组（不是对象/布尔）：Pydantic 层拒绝
    r = client.put("/documents/cfg/schema", json={"schema": [1, 2]})
    assert r.status_code == 422


# ---------------- 设置时的两类校验 ----------------


def test_invalid_schema_itself_rejected_and_not_stored(client):
    _create(client, {"a": 1, "b": "x"})
    r = _set_schema(client, "cfg", {"type": "definitely-not-a-type"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_schema"
    assert detail["errors"]
    # 非法 schema 没有落库
    assert client.get("/documents/cfg/schema").status_code == 404


def test_valid_schema_but_current_document_invalid_not_replaced(client):
    _create(client, {"a": "not-an-int"})
    r = _set_schema(client, "cfg", SCHEMA)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "schema_validation_failed"
    assert detail["revision"] == 1
    assert any(e["path"] == "/a" for e in detail["errors"])
    # 未创建契约
    assert client.get("/documents/cfg/schema").status_code == 404
    # 文档内容与修订号不变
    doc = client.get("/documents/cfg").json()
    assert doc["revision"] == 1
    assert doc["content"] == {"a": "not-an-int"}


def test_replace_existing_contract_failure_keeps_old_one(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    # 用旧契约允许的改动更新文档；新契约将与之不兼容
    assert _patch(client, "cfg", 1, "req-a", {"a": 5}).status_code == 200
    new_schema = {
        "type": "object",
        "properties": {"only": {"type": "integer"}},
        "required": ["only"],
        "additionalProperties": False,
    }
    r = _set_schema(client, "cfg", new_schema)
    assert r.status_code == 422
    # 旧契约仍在
    got = client.get("/documents/cfg/schema")
    assert got.status_code == 200
    assert got.json()["schema"] == SCHEMA
    # 旧契约仍生效（违反它的 PATCH 继续被拒）
    assert _patch(client, "cfg", 2, "req-bad", {"a": -1}).status_code == 422


def test_boolean_schemas_accepted(client):
    _create(client, {"anything": [1, 2, 3]})
    # true 是合法布尔 Schema，放行一切实例
    assert _set_schema(client, "cfg", True).status_code == 200
    assert _patch(client, "cfg", 1, "r1", {"x": None}).status_code == 200
    # false 也是**合法** Schema（元校验通过），但它拒绝一切实例：
    # 当前文档必不通过 → 422 schema_validation_failed 且契约保持 true
    r = _set_schema(client, "cfg", False)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "schema_validation_failed"
    got = client.get("/documents/cfg/schema")
    assert got.status_code == 200
    assert got.json()["schema"] is True


def test_idempotent_replay_with_contract_skips_revalidation(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    r1 = _patch(client, "cfg", 1, "idem", {"a": 7})
    assert r1.status_code == 200
    # 契约存在期间，同 request_id 重放仍直接幂等返回
    r = _patch(client, "cfg", 1, "idem", {"a": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent"] is True
    assert body["content"] == {"a": 7, "b": "x"}


# ---------------- PATCH 与契约 ----------------


def test_valid_patch_with_contract_succeeds(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    r = _patch(client, "cfg", 1, "req-ok", {"a": 42})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["content"] == {"a": 42, "b": "x"}


def test_invalid_patch_422_lists_all_errors_by_path(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    # a 变成字符串（type 失败）、b 被删除（required 失败）
    r = _patch(client, "cfg", 1, "req-bad", {"a": "oops", "b": None})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "schema_validation_failed"
    paths = [e["path"] for e in detail["errors"]]
    # 全部错误且按实例路径稳定排序："" 在 "/a" 之前
    assert paths == sorted(paths)
    assert "" in paths and "/a" in paths
    for e in detail["errors"]:
        assert set(e) == {"path", "validator", "message", "schema_path"}


def test_failed_validation_writes_no_revision_and_frees_request_id(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    before = client.get("/documents/cfg").json()

    r = _patch(client, "cfg", 1, "req-retry", {"a": -5})
    assert r.status_code == 422

    after = client.get("/documents/cfg").json()
    assert after == before
    # request_id 未被占用：同 ID 改发合法请求应成功
    r2 = _patch(client, "cfg", 1, "req-retry", {"a": 5})
    assert r2.status_code == 200, r2.text
    assert r2.json()["revision"] == 2
    # 再用同 ID 重放是幂等返回
    r3 = _patch(client, "cfg", 1, "req-retry", {"a": 5})
    assert r3.status_code == 200
    assert r3.json()["idempotent"] is True


def test_contract_checked_after_three_way_replay(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    # 服务端另一路 rev2：只改 b
    assert _patch(client, "cfg", 1, "srv", {"b": "y"}).status_code == 200
    # 来稿基于 rev1，与服务端路径不重叠，可重放；但候选值非法（a 为负）
    r = _patch(client, "cfg", 1, "cli-bad", {"a": -1})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "schema_validation_failed"
    assert any(e["path"] == "/a" for e in detail["errors"])
    # 仍停在 rev2
    assert client.get("/documents/cfg").json()["revision"] == 2

    # 合法的过期基线重放正常
    r = _patch(client, "cfg", 1, "cli-good", {"a": 9})
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is True
    assert body["content"] == {"a": 9, "b": "y"}


def test_conflict_takes_precedence_over_schema(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    # 服务端 rev2 改 a
    assert _patch(client, "cfg", 1, "srv", {"a": 10}).status_code == 200
    # 来稿也改 a 且非法：先做三方合并，路径冲突 → 409（不是 422）
    r = _patch(client, "cfg", 1, "cli", {"a": -1})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "conflict"


def test_delete_contract_restores_original_behavior(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    # additionalProperties=False：加 c 非法
    r = _patch(client, "cfg", 1, "req-add", {"c": 1})
    assert r.status_code == 422
    # 删除契约后同样的请求按原 Merge Patch 行为放行
    assert client.delete("/documents/cfg/schema").json()["deleted"] is True
    r = _patch(client, "cfg", 1, "req-add", {"c": 1})
    assert r.status_code == 200
    assert r.json()["content"] == {"a": 1, "b": "x", "c": 1}


def test_revalidate_stricter_contract_current_must_pass(client):
    _create(client, {"a": 1, "b": "x"})
    assert _set_schema(client, "cfg", SCHEMA).status_code == 200
    assert _patch(client, "cfg", 1, "req", {"a": 50}).status_code == 200
    strict = {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "minimum": 100},
            "b": {"type": "string"},
        },
        "required": ["a", "b"],
        "additionalProperties": False,
    }
    # 当前 a=50 不满足 minimum:100 → 替换被拒，旧契约保留
    r = _set_schema(client, "cfg", strict)
    assert r.status_code == 422
    got = client.get("/documents/cfg/schema").json()["schema"]
    assert got["properties"]["a"]["minimum"] == 0
