"""FastAPI 端到端集成测试（每个用例使用独立临时 SQLite 文件）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # db.connect() 每次读取 settings.db_path，直接改属性即可，
    # 避免 importlib.reload 导致模块内单例（如 MISSING）身份漂移
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _create(client, content=None, doc_id="cfg", author=None):
    body = {}
    if doc_id is not None:
        body["id"] = doc_id
    if content is not None:
        body["content"] = content
    if author is not None:
        body["author"] = author
    r = client.post("/documents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _patch(client, doc_id, base_revision, request_id, patch, author=None):
    body = {"base_revision": base_revision, "request_id": request_id, "patch": patch}
    if author is not None:
        body["author"] = author
    return client.patch(f"/documents/{doc_id}", json=body)


# ---------------- 创建 / 读取 ----------------


def test_create_and_get(client):
    doc = _create(client, {"a": 1, "nested": {"x": True}})
    assert doc["revision"] == 1
    r = client.get(f"/documents/{doc['id']}")
    assert r.status_code == 200
    assert r.json()["content"] == {"a": 1, "nested": {"x": True}}
    assert r.json()["revision"] == 1


def test_create_without_id_generates_one(client):
    r = client.post("/documents", json={"content": {"k": "v"}})
    assert r.status_code == 201
    assert r.json()["id"]


def test_create_duplicate_id_conflicts(client):
    _create(client, {"a": 1})
    r = client.post("/documents", json={"id": "cfg", "content": {}})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "document_exists"


def test_get_missing_returns_404(client):
    assert client.get("/documents/nope").status_code == 404


def test_invalid_id_pattern_rejected(client):
    r = client.post("/documents", json={"id": "bad id!"})
    assert r.status_code == 422


def test_unknown_field_rejected(client):
    r = client.post("/documents", json={"content": {}, "bogus": 1})
    assert r.status_code == 422


# ---------------- 快路径更新 ----------------


def test_fast_path_patch_creates_revision_2(client):
    _create(client, {"a": 1, "b": 2})
    r = _patch(client, "cfg", 1, "req-1", {"a": 9})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["content"] == {"a": 9, "b": 2}
    assert body["replayed"] is False
    assert body["changed"] is True
    assert body["idempotent"] is False
    assert client.get("/documents/cfg").json()["revision"] == 2


def test_null_deletes_key(client):
    _create(client, {"a": 1, "b": None})
    r = _patch(client, "cfg", 1, "req-del", {"a": None})
    assert r.status_code == 200
    assert r.json()["content"] == {"b": None}


def test_non_object_patch_replaces_document(client):
    _create(client, {"a": {"b": 1}})
    r = _patch(client, "cfg", 1, "req-rep", [1, 2, 3])
    assert r.status_code == 200
    assert r.json()["content"] == [1, 2, 3]
    # 再整体替换为 null
    r = _patch(client, "cfg", 2, "req-null", None)
    assert r.status_code == 200
    assert r.json()["content"] is None


def test_noop_patch_does_not_create_revision(client):
    _create(client, {"a": 1})
    # {"a": 1} 应用到 {"a": 1} 无变化
    r = _patch(client, "cfg", 1, "req-noop", {"a": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 1
    assert body["changed"] is False
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1]


# ---------------- 过期基线：自动重放 ----------------


def test_stale_base_disjoint_paths_auto_replays(client):
    _create(client, {"a": 1, "b": 2})
    # 服务端另一路写入：加 c → rev 2
    assert _patch(client, "cfg", 1, "server-add-c", {"c": 3}).status_code == 200
    # 来稿仍基于 rev 1，只改 a
    r = _patch(client, "cfg", 1, "client-change-a", {"a": 99})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 3
    assert body["replayed"] is True
    assert body["content"] == {"a": 99, "b": 2, "c": 3}
    # 修订历史记录了 replay 与基线
    rev3 = client.get("/documents/cfg/revisions/3").json()
    assert rev3["base_revision"] == 1
    assert rev3["replayed"] is True
    assert rev3["patch"] == {"a": 99}


def test_stale_base_same_result_no_new_revision(client):
    _create(client, {"a": 1, "b": 2})
    # 服务端已把 a 改成 99
    _patch(client, "cfg", 1, "srv", {"a": 99})
    # 来稿基于 rev1 也把 a 改成 99
    r = _patch(client, "cfg", 1, "cli-same", {"a": 99})
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 2
    assert body["replayed"] is True
    assert body["changed"] is False
    assert body["content"] == {"a": 99, "b": 2}


def test_stale_base_replay_with_null_delete(client):
    _create(client, {"a": 1, "b": 2, "c": 3})
    # 服务端加 d
    _patch(client, "cfg", 1, "srv-d", {"d": 4})
    # 来稿基于 rev1 删除 a
    r = _patch(client, "cfg", 1, "cli-del-a", {"a": None})
    assert r.status_code == 200
    assert r.json()["content"] == {"b": 2, "c": 3, "d": 4}


# ---------------- 冲突 ----------------


def test_stale_base_diverging_path_returns_409_and_writes_nothing(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "srv-a", {"a": 10})   # rev2: a=10
    before = client.get("/documents/cfg").json()

    r = _patch(client, "cfg", 1, "cli-a", {"a": 20})  # 来稿 a=20
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "conflict"
    assert detail["base_revision"] == 1
    assert detail["current_revision"] == 2
    conflict = detail["conflicts"][0]
    assert conflict["path"] == "/a"
    assert conflict["values"] == {"base": 1, "current": 10, "theirs": 20}
    assert conflict["missing"] == []

    # 不得写入：修订号与内容保持 rev2
    after = client.get("/documents/cfg").json()
    assert after == before
    # 冲突请求不占用 request_id（用户修正内容后可用同 ID 重试）
    r2 = _patch(client, "cfg", 2, "cli-a", {"b": 22})
    assert r2.status_code == 200


def test_nested_conflict_lists_pointer_and_three_values(client):
    _create(client, {"db": {"host": "h", "port": 5432}})
    _patch(client, "cfg", 1, "srv", {"db": {"port": 6543}})
    r = _patch(client, "cfg", 1, "cli", {"db": {"port": 3306}})
    assert r.status_code == 409
    c = r.json()["detail"]["conflicts"][0]
    assert c["path"] == "/db/port"
    assert c["values"] == {"base": 5432, "current": 6543, "theirs": 3306}


def test_delete_vs_edit_conflict(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "srv-del", {"a": None})
    r = _patch(client, "cfg", 1, "cli-edit", {"a": 2})
    assert r.status_code == 409
    c = r.json()["detail"]["conflicts"][0]
    assert c["path"] == "/a"
    assert c["values"] == {"base": 1, "current": None, "theirs": 2}
    assert c["missing"] == ["current"]


def test_unknown_base_revision_422(client):
    _create(client, {"a": 1})
    r = _patch(client, "cfg", 99, "req", {"a": 2})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "unknown_base_revision"


# ---------------- 幂等 ----------------


def test_same_request_id_same_content_is_idempotent(client):
    _create(client, {"a": 1})
    r1 = _patch(client, "cfg", 1, "idem-1", {"a": 7})
    assert r1.status_code == 200
    first = r1.json()
    # 再发一次，哪怕基线号已经过期
    r2 = _patch(client, "cfg", 1, "idem-1", {"a": 7})
    assert r2.status_code == 200
    second = r2.json()
    assert second["idempotent"] is True
    assert second["revision"] == first["revision"] == 2
    assert second["content"] == first["content"]
    # 没有产生额外修订
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2]


def test_same_request_id_different_content_conflicts(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "dup", {"a": 7}).status_code == 200
    # 同 request_id，不同 patch
    r = _patch(client, "cfg", 1, "dup", {"a": 8})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"
    # 同 request_id，不同 base_revision
    r2 = _patch(client, "cfg", 2, "dup", {"a": 7})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "request_id_conflict"


def test_idempotent_noop_then_history(client):
    _create(client, {"a": 1})
    r1 = _patch(client, "cfg", 1, "noop-idem", {"a": 1})
    assert r1.json()["changed"] is False
    r2 = _patch(client, "cfg", 1, "noop-idem", {"a": 1})
    assert r2.status_code == 200
    assert r2.json()["idempotent"] is True
    assert r2.json()["changed"] is False
    assert r2.json()["revision"] == 1


# ---------------- 历史 ----------------


def test_revision_history_order_and_pagination(client):
    _create(client, {"v": 0})
    for i in range(1, 4):
        assert _patch(client, "cfg", i, f"r{i}", {"v": i}).status_code == 200
    asc = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in asc] == [1, 2, 3, 4]
    page = client.get("/documents/cfg/revisions?limit=2&offset=1&order=desc").json()["items"]
    assert [h["revision"] for h in page] == [3, 2]


def test_history_of_missing_document_404(client):
    assert client.get("/documents/ghost/revisions").status_code == 404
    assert client.get("/documents/cfg/revisions/99").status_code == 404


def test_full_workflow_with_health(client):
    assert client.get("/health").status_code == 200
    _create(client, {"feature": {"enabled": False, "ratio": 0}})
    _patch(client, "cfg", 1, "f1", {"feature": {"ratio": 50}})
    _patch(client, "cfg", 2, "f2", {"feature": {"enabled": True}})
    final = client.get("/documents/cfg").json()
    assert final["content"] == {"feature": {"enabled": True, "ratio": 50}}
    assert final["revision"] == 3
