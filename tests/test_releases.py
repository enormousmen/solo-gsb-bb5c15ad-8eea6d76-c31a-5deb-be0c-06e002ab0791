"""POST /releases 原子批量发布集成测试（每个用例使用独立临时数据库）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _create(client, content, doc_id, author=None):
    body = {"id": doc_id, "content": content}
    if author is not None:
        body["author"] = author
    r = client.post("/documents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _item(doc_id, base_revision, patch, author=None):
    it = {"document_id": doc_id, "base_revision": base_revision, "patch": patch}
    if author is not None:
        it["author"] = author
    return it


def _release(client, release_id, items):
    return client.post("/releases", json={"release_id": release_id, "items": items})


def _patch(client, doc_id, base_revision, request_id, patch):
    return client.patch(
        f"/documents/{doc_id}",
        json={"base_revision": base_revision, "request_id": request_id, "patch": patch},
    )


SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer", "minimum": 0}},
    "required": ["a"],
    "additionalProperties": False,
}


# ---------------- 成功路径 ----------------


def test_release_happy_path_writes_all_documents_atomically(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"x": 1}, "d2")
    r = _release(
        client,
        "rel-1",
        [
            _item("d1", 1, {"a": 100}, author="alice"),
            _item("d2", 1, {"x": 2, "y": 3}),
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["release_id"] == "rel-1"
    assert body["idempotent"] is False
    assert "created_at" in body
    # 按输入顺序返回各项最终 revision / content
    assert [it["document_id"] for it in body["items"]] == ["d1", "d2"]
    it1, it2 = body["items"]
    assert it1["revision"] == 2 and it1["changed"] is True
    assert it1["replayed"] is False
    assert it1["content"] == {"a": 100}
    assert it2["revision"] == 2 and it2["content"] == {"x": 2, "y": 3}
    # 整批同一时间戳
    assert it1["created_at"] == it2["created_at"] == body["created_at"]
    # 各文档状态已落库
    assert client.get("/documents/d1").json()["content"] == {"a": 100}
    assert client.get("/documents/d2").json()["revision"] == 2


def test_release_revisions_traceable_in_history(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    r = _release(
        client, "rel-hist", [_item("d1", 1, {"a": 2}, author="bob"), _item("d2", 1, {"b": 9})]
    )
    assert r.status_code == 200, r.text
    rev2 = client.get("/documents/d1/revisions/2").json()
    # 发布产生的修订以 release_id 作为 request_id，可追溯
    assert rev2["request_id"] == "rel-hist"
    assert rev2["author"] == "bob"
    assert rev2["base_revision"] == 1
    assert rev2["patch"] == {"a": 2}
    hist = client.get("/documents/d2/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2]


def test_release_reuses_three_way_merge_for_stale_base(client):
    _create(client, {"a": 1, "b": 2}, "d1")
    _create(client, {"z": 0}, "d2")
    # 服务端另一路先把 d1 加了 c → rev2
    assert _patch(client, "d1", 1, "srv", {"c": 3}).status_code == 200
    # 发布项仍基于 rev1，只改 b：路径不重叠 → 自动重放
    r = _release(client, "rel-replay", [_item("d1", 1, {"b": 99}), _item("d2", 1, {"z": 1})])
    assert r.status_code == 200, r.text
    it1 = r.json()["items"][0]
    assert it1["replayed"] is True
    assert it1["revision"] == 3
    assert it1["content"] == {"a": 1, "b": 99, "c": 3}


def test_release_noop_item_creates_no_revision_but_release_succeeds(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    r = _release(client, "rel-noop", [_item("d1", 1, {"a": 1}), _item("d2", 1, {"b": 2})])
    assert r.status_code == 200, r.text
    it1, it2 = r.json()["items"]
    assert it1["changed"] is False and it1["revision"] == 1
    assert it2["changed"] is True and it2["revision"] == 2
    hist = client.get("/documents/d1/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1]


# ---------------- 请求形状校验 ----------------


def test_release_batch_size_limits(client):
    _create(client, {"a": 1}, "d1")
    # 0 / 1 项 → 422（单项请用 PATCH /documents/{id}）
    assert _release(client, "rel-0", []).status_code == 422
    assert _release(client, "rel-1", [_item("d1", 1, {"a": 2})]).status_code == 422
    # 21 项 → 422
    items = [_item("d1", 1, {"a": 2})] + [
        _item(f"doc-{i}", 1, {}) for i in range(20)
    ]
    assert _release(client, "rel-21", items).status_code == 422


def test_release_batch_size_upper_boundary_ok(client):
    for i in range(20):
        _create(client, {"v": 0}, f"doc-{i}")
    items = [_item(f"doc-{i}", 1, {"v": i}) for i in range(20)]
    r = _release(client, "rel-20", items)
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 20


def test_release_duplicate_document_id_rejected(client):
    _create(client, {"a": 1}, "d1")
    r = _release(client, "rel-dup", [_item("d1", 1, {"a": 2}), _item("d1", 1, {"a": 3})])
    assert r.status_code == 422


def test_release_unknown_fields_rejected(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    r = client.post(
        "/releases",
        json={
            "release_id": "rel-x",
            "items": [_item("d1", 1, {"a": 2}), _item("d2", 1, {"b": 2})],
            "bogus": 1,
        },
    )
    assert r.status_code == 422
    bad_item = _item("d1", 1, {"a": 2})
    bad_item["bogus"] = 1
    r = _release(client, "rel-y", [bad_item, _item("d2", 1, {"b": 2})])
    assert r.status_code == 422


# ---------------- 整批失败与回滚 ----------------


def test_release_missing_document_rolls_back_and_frees_release_id(client):
    _create(client, {"a": 1}, "d1")
    r = _release(client, "rel-ghost", [_item("d1", 1, {"a": 2}), _item("ghost", 1, {"x": 1})])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "release_failed"
    assert detail["release_id"] == "rel-ghost"
    assert len(detail["failures"]) == 1
    fail = detail["failures"][0]
    assert fail["index"] == 1
    assert fail["document_id"] == "ghost"
    assert fail["error"] == "not_found"
    # 整批回滚：d1 不产生修订
    assert client.get("/documents/d1").json()["revision"] == 1
    # 失败不占用 release_id：修正后可用同 ID 重发
    _create(client, {"x": 0}, "ghost")
    r2 = _release(client, "rel-ghost", [_item("d1", 1, {"a": 2}), _item("ghost", 1, {"x": 1})])
    assert r2.status_code == 200, r2.text


def test_release_unknown_base_revision_failure(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    r = _release(client, "rel-base", [_item("d1", 1, {"a": 2}), _item("d2", 99, {"b": 2})])
    assert r.status_code == 422
    fail = r.json()["detail"]["failures"][0]
    assert fail["index"] == 1
    assert fail["error"] == "unknown_base_revision"
    assert fail["base_revision"] == 99
    assert fail["current_revision"] == 1
    assert client.get("/documents/d1").json()["revision"] == 1


def test_release_conflict_rolls_back_all_documents(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    # 服务端另一路把 d1.a 改成 10 → rev2
    assert _patch(client, "d1", 1, "srv", {"a": 10}).status_code == 200
    before_d1 = client.get("/documents/d1").json()
    before_d2 = client.get("/documents/d2").json()

    r = _release(client, "rel-conflict", [_item("d1", 1, {"a": 20}), _item("d2", 1, {"b": 2})])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "release_failed"
    fail = detail["failures"][0]
    assert fail["index"] == 0
    assert fail["error"] == "conflict"
    assert fail["current_revision"] == 2
    conflict = fail["conflicts"][0]
    assert conflict["path"] == "/a"
    assert conflict["values"] == {"base": 1, "current": 10, "theirs": 20}
    # 所有文档均不得产生修订
    assert client.get("/documents/d1").json() == before_d1
    assert client.get("/documents/d2").json() == before_d2


def test_release_contract_violation_rolls_back(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    assert client.put("/documents/d1/schema", json={"schema": SCHEMA}).status_code == 200
    r = _release(client, "rel-schema", [_item("d1", 1, {"a": -5}), _item("d2", 1, {"b": 2})])
    assert r.status_code == 422
    fail = r.json()["detail"]["failures"][0]
    assert fail["index"] == 0
    assert fail["error"] == "schema_validation_failed"
    assert any(e["path"] == "/a" for e in fail["errors"])
    assert client.get("/documents/d1").json()["revision"] == 1
    assert client.get("/documents/d2").json()["revision"] == 1


def test_release_aggregates_multiple_failures_in_input_order(client):
    _create(client, {"a": 1}, "ok1")
    _create(client, {"b": 1}, "badbase")
    r = _release(
        client,
        "rel-multi",
        [
            _item("ghost-1", 1, {"x": 1}),
            _item("ok1", 1, {"a": 2}),
            _item("ghost-2", 1, {"y": 1}),
            _item("badbase", 42, {"b": 2}),
        ],
    )
    assert r.status_code == 422
    failures = r.json()["detail"]["failures"]
    assert [(f["index"], f["document_id"], f["error"]) for f in failures] == [
        (0, "ghost-1", "not_found"),
        (2, "ghost-2", "not_found"),
        (3, "badbase", "unknown_base_revision"),
    ]
    # 全部回滚
    assert client.get("/documents/ok1").json()["revision"] == 1
    assert client.get("/documents/badbase").json()["revision"] == 1


# ---------------- 整批幂等 ----------------


def test_release_idempotent_replay_returns_original_result(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    items = [_item("d1", 1, {"a": 7}), _item("d2", 1, {"b": 8})]
    r1 = _release(client, "rel-idem", items)
    assert r1.status_code == 200
    first = r1.json()

    r2 = _release(client, "rel-idem", items)
    assert r2.status_code == 200
    second = r2.json()
    assert second["idempotent"] is True
    # 除 idempotent 标记外与原结果完全一致
    assert {k: v for k, v in second.items() if k != "idempotent"} == {
        k: v for k, v in first.items() if k != "idempotent"
    }
    # 没有产生额外修订
    assert [h["revision"] for h in client.get("/documents/d1/revisions").json()["items"]] == [1, 2]
    assert [h["revision"] for h in client.get("/documents/d2/revisions").json()["items"]] == [1, 2]


def test_release_same_id_different_payload_conflicts(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    assert _release(client, "rel-dup", [_item("d1", 1, {"a": 2}), _item("d2", 1, {"b": 2})]).status_code == 200
    # 同 release_id，不同 patch
    r = _release(client, "rel-dup", [_item("d1", 1, {"a": 3}), _item("d2", 1, {"b": 2})])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "release_id_conflict"
    # 同 release_id，仅 author 不同也算不同请求
    r = _release(
        client,
        "rel-dup",
        [_item("d1", 1, {"a": 2}, author="mallory"), _item("d2", 1, {"b": 2})],
    )
    assert r.status_code == 409
    # 文档状态未被 409 请求改变
    assert client.get("/documents/d1").json()["content"] == {"a": 2}


def test_release_idempotency_persists_across_restarts(client, tmp_path, monkeypatch):
    # releases 表落库：换一个连接（模拟重启后）重放仍命中
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    items = [_item("d1", 1, {"a": 5}), _item("d2", 1, {"b": 6})]
    assert _release(client, "rel-persist", items).status_code == 200
    with TestClient(app) as c2:  # 同一 db 文件，新客户端（重跑 lifespan）
        r = c2.post("/releases", json={"release_id": "rel-persist", "items": items})
        assert r.status_code == 200
        assert r.json()["idempotent"] is True
        assert [h["revision"] for h in c2.get("/documents/d1/revisions").json()["items"]] == [1, 2]


# ---------------- 与单文档接口共存 ----------------


def test_release_then_single_document_patch_stays_compatible(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    assert _release(client, "rel-mix", [_item("d1", 1, {"a": 2}), _item("d2", 1, {"b": 2})]).status_code == 200
    # 单文档接口基于发布后的新修订继续工作
    r = _patch(client, "d1", 2, "req-after", {"a": 3})
    assert r.status_code == 200
    assert r.json()["revision"] == 3
    assert r.json()["content"] == {"a": 3}
    # 单文档接口的 request_id 幂等不受 release_id 影响
    r = _patch(client, "d1", 2, "req-after", {"a": 3})
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
