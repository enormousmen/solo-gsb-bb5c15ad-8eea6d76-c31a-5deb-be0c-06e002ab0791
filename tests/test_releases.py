"""POST /releases 原子发布端到端集成测试（每个用例使用独立临时数据库）。"""
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


def _release(client, release_id, updates):
    return client.post(
        "/releases", json={"release_id": release_id, "updates": updates}
    )


def _item(doc_id, base_revision, patch, author=None):
    body = {"document_id": doc_id, "base_revision": base_revision, "patch": patch}
    if author is not None:
        body["author"] = author
    return body


def _revision(client, doc_id, revision):
    return client.get(f"/documents/{doc_id}/revisions/{revision}").json()


# ---------------- 基本成功路径 ----------------


def test_release_commits_all_documents_atomically(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")
    _create(client, {"a": 1}, "d3")

    r = _release(client, "rel-1", [
        _item("d1", 1, {"a": 10}),
        _item("d2", 1, {"a": 20}, author="bob"),
        _item("d3", 1, {"a": 30}),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["release_id"] == "rel-1"
    assert body["idempotent"] is False
    assert [x["document_id"] for x in body["results"]] == ["d1", "d2", "d3"]

    for doc_id, value in [("d1", 10), ("d2", 20), ("d3", 30)]:
        item = next(x for x in body["results"] if x["document_id"] == doc_id)
        assert item["revision"] == 2
        assert item["base_revision"] == 1
        assert item["content"] == {"a": value}
        assert item["replayed"] is False
        assert item["changed"] is True
        doc = client.get(f"/documents/{doc_id}").json()
        assert doc["revision"] == 2
        assert doc["content"] == {"a": value}

    # 修订以 release_id 作为 request_id 落库，可追溯到整批发布
    rev = _revision(client, "d2", 2)
    assert rev["request_id"] == "rel-1"
    assert rev["author"] == "bob"
    assert rev["replayed"] is False


def test_release_results_order_matches_input_order(client):
    for name in ("zeta", "alpha", "mid"):
        _create(client, {"v": 0}, name)
    r = _release(client, "r", [
        _item("zeta", 1, {"v": 1}),
        _item("alpha", 1, {"v": 2}),
        _item("mid", 1, {"v": 3}),
    ])
    assert [x["document_id"] for x in r.json()["results"]] == ["zeta", "alpha", "mid"]


def test_release_minimum_two_items(client):
    _create(client, {}, "d1")
    # 仅 1 项：违反每批 2~20 项约束
    r = _release(client, "r", [_item("d1", 1, {"x": 1})])
    assert r.status_code == 422


def test_release_rejects_duplicate_document_ids(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")
    r = _release(client, "r", [
        _item("d1", 1, {"a": 2}),
        _item("d2", 1, {"a": 2}),
        _item("d1", 1, {"a": 3}),
    ])
    assert r.status_code == 422
    # 任何文档都未被修改
    assert client.get("/documents/d1").json()["revision"] == 1


def test_release_accepts_twenty_items(client):
    for i in range(20):
        _create(client, {"v": 0}, f"d{i}")
    r = _release(client, "r", [_item(f"d{i}", 1, {"v": i}) for i in range(20)])
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) == 20


def test_release_rejects_twenty_one_items(client):
    for i in range(21):
        _create(client, {"v": 0}, f"d{i}")
    r = _release(client, "r", [_item(f"d{i}", 1, {"v": i}) for i in range(21)])
    assert r.status_code == 422


def test_release_rejects_extra_field(client):
    _create(client, {}, "d1")
    _create(client, {}, "d2")
    r = client.post("/releases", json={
        "release_id": "r",
        "updates": [
            {"document_id": "d1", "base_revision": 1, "patch": {}, "bogus": 1},
            {"document_id": "d2", "base_revision": 1, "patch": {}},
        ],
    })
    assert r.status_code == 422


# ---------------- 失败聚合与原子性 ----------------


def test_release_aggregates_all_failures_in_input_order_and_writes_nothing(client):
    _create(client, {"a": 1}, "ok1")
    _create(client, {"a": 1}, "bad-base")
    _create(client, {"a": 1, "b": 2}, "conflict")
    _create(client, {"a": 1}, "ok2")
    # 让 conflict 文档走到真正的三方分歧：服务端先改 a
    cp = client.patch("/documents/conflict",
                      json={"base_revision": 1, "request_id": "srv", "patch": {"a": 10}})
    assert cp.status_code == 200

    before = {d: client.get(f"/documents/{d}").json()
              for d in ("ok1", "bad-base", "conflict", "ok2")}

    r = _release(client, "rel-fail", [
        _item("ok1", 1, {"a": 100}),                          # 0 成功
        _item("missing-doc", 1, {"a": 1}),                    # 1 not_found
        _item("bad-base", 99, {"a": 2}),                      # 2 unknown_base_revision
        _item("conflict", 1, {"a": 20}),                      # 3 conflict
        _item("ok2", 1, {"a": 200}),                          # 4 成功
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "release_failed"
    assert detail["release_id"] == "rel-fail"
    failures = detail["failures"]
    assert [f["index"] for f in failures] == [1, 2, 3]
    by_index = {f["index"]: f for f in failures}
    assert by_index[1]["error"] == "not_found"
    assert by_index[1]["document_id"] == "missing-doc"
    assert by_index[1]["status_code"] == 404
    assert by_index[2]["error"] == "unknown_base_revision"
    assert by_index[2]["base_revision"] == 99
    assert by_index[2]["current_revision"] == 1
    assert by_index[2]["status_code"] == 422
    assert by_index[3]["error"] == "conflict"
    assert by_index[3]["conflicts"][0]["path"] == "/a"
    assert by_index[3]["status_code"] == 409

    # 即使是本可成功的项也不得落库：全部文档保持原状
    after = {d: client.get(f"/documents/{d}").json()
             for d in ("ok1", "bad-base", "conflict", "ok2")}
    assert after == before


def test_release_failure_does_not_consume_release_id(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")

    r1 = _release(client, "rel-retry", [
        _item("d1", 1, {"a": 2}),
        _item("ghost", 1, {"a": 2}),
    ])
    assert r1.status_code == 422

    # 同一 release_id 修正为合法请求后应成功（失败未占用 ID）
    r2 = _release(client, "rel-retry", [
        _item("d1", 1, {"a": 2}),
        _item("d2", 1, {"a": 3}),
    ])
    assert r2.status_code == 200, r2.text
    assert r2.json()["idempotent"] is False
    assert client.get("/documents/d1").json()["content"] == {"a": 2}


def test_release_contract_validation_failure_aborts_whole_batch(client):
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer", "minimum": 0}},
        "required": ["a"],
        "additionalProperties": False,
    }
    _create(client, {"a": 1}, "good")
    _create(client, {"a": 1}, "bound")
    client.put("/documents/bound/schema", json={"schema": schema})

    before_good = client.get("/documents/good").json()
    r = _release(client, "rel-schema", [
        _item("good", 1, {"a": 99}),
        _item("bound", 1, {"a": -5}),
    ])
    assert r.status_code == 422
    failures = r.json()["detail"]["failures"]
    assert len(failures) == 1
    assert failures[0]["index"] == 1
    assert failures[0]["error"] == "schema_validation_failed"
    assert any(e["path"] == "/a" for e in failures[0]["errors"])
    # 同批第一项也回滚
    assert client.get("/documents/good").json() == before_good
    assert client.get("/documents/bound").json()["revision"] == 1
    # release_id 未被占用
    r2 = _release(client, "rel-schema", [
        _item("good", 1, {"a": 99}),
        _item("bound", 1, {"a": 5}),
    ])
    assert r2.status_code == 200


# ---------------- 过期基线 / 三方重放 ----------------


def test_release_replays_stale_bases(client):
    _create(client, {"a": 1, "b": 2}, "d1")
    _create(client, {"x": 1, "y": 2}, "d2")
    # 服务端另路写入，使来稿基线过期
    assert client.patch("/documents/d1", json={
        "base_revision": 1, "request_id": "s1", "patch": {"c": 3}
    }).status_code == 200
    assert client.patch("/documents/d2", json={
        "base_revision": 1, "request_id": "s2", "patch": {"z": 9}
    }).status_code == 200

    r = _release(client, "rel-replay", [
        _item("d1", 1, {"a": 100}),
        _item("d2", 1, {"x": 100}),
    ])
    assert r.status_code == 200, r.text
    results = {x["document_id"]: x for x in r.json()["results"]}
    assert results["d1"]["replayed"] is True
    assert results["d1"]["content"] == {"a": 100, "b": 2, "c": 3}
    assert results["d1"]["revision"] == 3
    assert results["d2"]["replayed"] is True
    assert results["d2"]["content"] == {"x": 100, "y": 2, "z": 9}


def test_release_noop_item_creates_no_revision(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")
    r = _release(client, "rel-noop", [
        _item("d1", 1, {"a": 1}),   # 无变化
        _item("d2", 1, {"a": 2}),
    ])
    assert r.status_code == 200
    results = {x["document_id"]: x for x in r.json()["results"]}
    assert results["d1"]["revision"] == 1
    assert results["d1"]["changed"] is False
    assert results["d2"]["revision"] == 2
    # 无变化项不产生新修订
    assert [h["revision"] for h in
            client.get("/documents/d1/revisions").json()["items"]] == [1]


def test_release_two_items_on_same_document_not_allowed(client):
    # 同一文档在一批内出现两次属于 document_id 重复（避免批内自冲突的歧义）
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")
    r = _release(client, "r", [
        _item("d1", 1, {"a": 2}),
        _item("d1", 1, {"b": 3}),
        _item("d2", 1, {"a": 2}),
    ])
    assert r.status_code == 422


# ---------------- 幂等 ----------------


def test_release_same_request_replays_original_result(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")
    req = [_item("d1", 1, {"a": 7}), _item("d2", 1, {"a": 8})]
    r1 = _release(client, "rel-idem", req)
    assert r1.status_code == 200
    first = r1.json()

    # 基线号此时均已过期；重放必须返回原结果而非重新合并
    r2 = _release(client, "rel-idem", req)
    assert r2.status_code == 200
    second = r2.json()
    assert second["idempotent"] is True
    assert second["results"] == first["results"]
    # 没有产生额外修订
    assert [h["revision"] for h in
            client.get("/documents/d1/revisions").json()["items"]] == [1, 2]
    assert [h["revision"] for h in
            client.get("/documents/d2/revisions").json()["items"]] == [1, 2]


def test_release_replay_is_insensitive_to_json_whitespace_and_key_order(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")
    r1 = client.post("/releases", content=(
        b'{"release_id":"rel-norm","updates":['
        b'{"document_id":"d1","base_revision":1,"patch":{"a":7}},'
        b'{"document_id":"d2","base_revision":1,"patch":{"a":8}}]}'
    ), headers={"Content-Type": "application/json"})
    assert r1.status_code == 200
    # 键顺序不同、author 显式给 null：规范化后语义相同
    r2 = client.post("/releases", json={
        "updates": [
            {"patch": {"a": 7}, "base_revision": 1, "document_id": "d1", "author": None},
            {"document_id": "d2", "patch": {"a": 8}, "base_revision": 1, "author": None},
        ],
        "release_id": "rel-norm",
    })
    assert r2.status_code == 200
    assert r2.json()["idempotent"] is True


def test_release_same_id_different_request_returns_409(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")
    assert _release(client, "rel-dup", [
        _item("d1", 1, {"a": 7}), _item("d2", 1, {"a": 8})
    ]).status_code == 200

    # patch 不同
    r = _release(client, "rel-dup", [
        _item("d1", 1, {"a": 9}), _item("d2", 1, {"a": 8})
    ])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "release_id_conflict"

    # base_revision 不同
    r = _release(client, "rel-dup", [
        _item("d1", 2, {"a": 7}), _item("d2", 1, {"a": 8})
    ])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "release_id_conflict"

    # 文档集合 / 顺序不同
    r = _release(client, "rel-dup", [
        _item("d2", 1, {"a": 8}), _item("d1", 1, {"a": 7})
    ])
    assert r.status_code == 409

    # 冲突重放不得再写任何修订
    assert client.get("/documents/d1").json()["revision"] == 2


def test_release_id_independent_of_single_document_request_id(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"a": 1}, "d2")
    # 单文档 PATCH 已用 "shared-id"
    assert client.patch("/documents/d1", json={
        "base_revision": 1, "request_id": "shared-id", "patch": {"a": 5}
    }).status_code == 200
    # 发布用同名 release_id 不冲突（两套幂等命名空间独立）
    r = _release(client, "shared-id", [
        _item("d1", 2, {"a": 6}), _item("d2", 1, {"a": 6})
    ])
    assert r.status_code == 200, r.text
