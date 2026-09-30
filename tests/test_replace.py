"""封存后替换见证人功能测试。

规则要点：
- 仅封存人可在资料未开封时替换；新见证人须已注册、非封存人、不在当前名单，
  被替换者须在当前名单；替换后仍为三名不同见证人；
- 无进行中的开封请求时可直接替换；
- 有进行中请求时：被替换者对本次请求的同意失效、其他同意保留；
  有效票不足两票时请求退回 pending，新见证人凭封存摘要为本次请求投票即可开封；
- 到最早开封时刻且两票已齐的请求先开封并拒绝替换（开封先落库）；
- 已过截止时刻仍 pending 的请求按原规则失效，替换照常完成；
- 旧见证人随即失去资料访问与同意权限；变更留审计记录；
- 任何拒绝路径不改状态、不泄露正文。
"""
import datetime as dt

from conftest import auth, digest_of, register, seal

BODY = "替换见证人绝密资料 REPLACE-WITNESS-SECRET"


def _deadline(minutes: int = 30) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)).isoformat()


def _iso(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def _setup(client, names=("owner", "w1", "w2", "w3", "w4")):
    owner = register(client, names[0])
    keys = [register(client, n) for n in names[1:]]
    d = seal(client, owner, BODY, list(names[1:4]))
    return owner, keys, d["document_id"], d["digest"]


def _replace(client, owner, doc_id, old, new):
    return client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"replacee": old, "new_witness": new},
        headers=auth(owner),
    )


def _create_request(client, owner, doc_id, *, deadline_in=1800, earliest_in=None):
    payload = {"reason": "替换测试", "deadline": _iso(deadline_in)}
    if earliest_in is not None:
        payload["earliest_open_at"] = _iso(earliest_in)
    return client.post(
        f"/documents/{doc_id}/open-requests", json=payload, headers=auth(owner)
    )


# ---------------------------------------------------------------------------
# 校验与权限
# ---------------------------------------------------------------------------


def test_replace_basic_without_active_request(client):
    owner, (w1, w2, w3, w4), doc_id, _ = _setup(client)

    r = _replace(client, owner, doc_id, "w1", "w4")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["witnesses"] == ["w4", "w2", "w3"]
    assert data["status"] == "sealed"
    assert data["replaced"] == {"from": "w1", "to": "w4"}
    assert data["open_request"] is None

    # 查询接口返回的是新名单
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["witnesses"] == ["w4", "w2", "w3"]


def test_replace_validation_and_permissions(client):
    owner, (w1, w2, w3, w4), doc_id, _ = _setup(client)
    register(client, "ghost-witness")  # 仅占用名字，下面用未注册名

    # 非封存人（见证人本人）不能替换
    assert _replace(client, w1, doc_id, "w1", "w4").status_code == 403
    # 非参与方不可见，统一 403
    outsider = register(client, "outsider-r")
    assert _replace(client, outsider, doc_id, "w1", "w4").status_code == 403
    # 缺少 API Key -> 401
    assert client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"replacee": "w1", "new_witness": "w4"},
    ).status_code == 401

    # 被替换者不在名单
    assert _replace(client, owner, doc_id, "w4", "w1").status_code == 400
    # 新见证人已在名单
    assert _replace(client, owner, doc_id, "w1", "w2").status_code == 400
    # 新旧相同
    assert _replace(client, owner, doc_id, "w1", "w1").status_code == 400
    # 新见证人未注册
    assert _replace(client, owner, doc_id, "w1", "nobody-here").status_code == 400
    # 新见证人是封存人
    assert _replace(client, owner, doc_id, "w1", "owner").status_code == 400

    # 全部拒绝后名单不变
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["witnesses"] == ["w1", "w2", "w3"]

    # 字段缺失 -> 422，且不回显正文
    r = client.post(
        f"/documents/{doc_id}/witness-replacements",
        json={"new_witness": "w4"},
        headers=auth(owner),
    )
    assert r.status_code == 422 and BODY not in r.text


def test_old_witness_loses_access_and_approval_rights(client):
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    req = _create_request(client, owner, doc_id).json()["request_id"]

    assert _replace(client, owner, doc_id, "w1", "w4").status_code == 200

    # 旧见证人查不到资料
    assert client.get(f"/documents/{doc_id}", headers=auth(w1)).status_code == 403
    # 旧见证人不能再同意
    r = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))
    assert r.status_code == 403

    # 新见证人可访问、可同意
    assert client.get(f"/documents/{doc_id}", headers=auth(w4)).status_code == 200


def test_replace_on_opened_document_rejected(client):
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    req = _create_request(client, owner, doc_id).json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2))

    r = _replace(client, owner, doc_id, "w3", "w4")
    assert r.status_code == 409
    # 名单不变，且错误体不含正文
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["witnesses"] == ["w1", "w2", "w3"]
    assert BODY not in r.text


# ---------------------------------------------------------------------------
# 有进行中开封请求：同意失效 / 保留 / 退回待同意
# ---------------------------------------------------------------------------


def test_replace_non_voter_keeps_vote_then_new_witness_completes(client):
    """被替换者未投票：已有票数原样保留；新见证人凭封存摘要补上第二票即开封。"""
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    req = _create_request(client, owner, doc_id).json()["request_id"]
    # w3 未投票；只有 w1 一票
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))

    r = _replace(client, owner, doc_id, "w3", "w4")
    assert r.status_code == 200, r.text
    info = r.json()["open_request"]
    assert info["request_id"] == req and info["status"] == "pending"
    assert info["approval_count"] == 1

    # w4 凭封存摘要补上第二票 -> 立即开封（沿用原请求）
    r = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w4))
    assert r.status_code == 200 and r.json()["status"] == "opened"
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["body"] == BODY
    names = [a["username"] for a in got["witness_approvals"]]
    assert names == ["w1", "w4"]


def test_replace_voter_drops_count_request_back_to_pending(client):
    """被替换者已投票：删除其同意，剩票保留；新见证人投票后与保留票凑齐两票开封。"""
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    d2 = seal(client, owner, BODY + "2", ["w1", "w2", "w3"])
    doc2, dg2 = d2["document_id"], d2["digest"]
    req2 = _create_request(client, owner, doc2).json()["request_id"]
    client.post(f"/open-requests/{req2}/approve", json={"digest": dg2}, headers=auth(w1))

    r = _replace(client, owner, doc2, "w1", "w4")
    assert r.status_code == 200, r.text
    info = r.json()["open_request"]
    assert info["status"] == "pending" and info["approval_count"] == 0

    # w4 投票 + w2 保留票凑齐
    r = client.post(f"/open-requests/{req2}/approve", json={"digest": dg2}, headers=auth(w4))
    assert r.json()["approval_count"] == 1 and r.json()["status"] == "pending"
    r = client.post(f"/open-requests/{req2}/approve", json={"digest": dg2}, headers=auth(w2))
    assert r.json()["status"] == "opened"
    got = client.get(f"/documents/{doc2}", headers=auth(owner)).json()
    assert got["body"] == BODY + "2"


def test_replace_in_waiting_before_earliest_back_to_pending(client):
    """两票在最早开封时刻前集齐（waiting）：替换其中一名投票见证人后
    有效票不足两票，请求退回 pending；新见证人凭摘要投票即重新集齐两票，
    到点前仍是 waiting。"""
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    r = _create_request(client, owner, doc_id, earliest_in=3600, deadline_in=7200)
    req = r.json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))
    r = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.json()["status"] == "waiting"

    r = _replace(client, owner, doc_id, "w2", "w4")
    assert r.status_code == 200, r.text
    info = r.json()["open_request"]
    assert info["status"] == "pending" and info["approval_count"] == 1
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["latest_open_request"]["status"] == "pending"
    assert got["latest_open_request"]["approval_count"] == 1
    assert "body" not in got

    # 新见证人投票：重新集齐两票 -> waiting（未到最早时刻）
    r = client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w4))
    assert r.status_code == 200 and r.json()["status"] == "waiting"
    # 旧见证人 w2 已无权
    assert client.post(
        f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2)
    ).status_code == 403


def test_replace_third_nonvoter_keeps_waiting(client):
    """waiting 下替换未投票的第三见证人：两票保留，请求仍为 waiting。"""
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    r = _create_request(client, owner, doc_id, earliest_in=3600, deadline_in=7200)
    req = r.json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2))

    r = _replace(client, owner, doc_id, "w3", "w4")
    assert r.status_code == 200, r.text
    info = r.json()["open_request"]
    assert info["status"] == "waiting" and info["approval_count"] == 2


# ---------------------------------------------------------------------------
# 到点必开封优先 / 过期请求
# ---------------------------------------------------------------------------


def test_replace_rejected_when_waiting_due_opens_first(client, sweep):
    """到最早开封时刻且两票已齐：替换必须先开封并被拒绝（开封先落库）。"""
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "到点替换赛跑", "deadline": _iso(3600),
              "earliest_open_at": _iso(1)},
        headers=auth(owner),
    )
    req = r.json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w2))

    import time

    time.sleep(1.2)  # 不主动 sweep，由替换写事务做确定性到点开封
    r = _replace(client, owner, doc_id, "w3", "w4")
    assert r.status_code == 409, r.text
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "opened" and got["body"] == BODY
    # 名单不变（替换未生效）
    assert got["witnesses"] == ["w1", "w2", "w3"]
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    assert [x["action"] for x in records].count("opened") == 1
    assert "witness_replaced" not in [x["action"] for x in records]


def test_replace_after_deadline_expires_request_then_succeeds(client, sweep):
    """已过截止时刻仍 pending：按原规则失效，替换照常完成，新请求从 0 票开始。"""
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    r = client.post(
        f"/documents/{doc_id}/open-requests",
        json={"reason": "拖到期", "deadline": _iso(1)},
        headers=auth(owner),
    )
    req = r.json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))

    import time

    time.sleep(1.2)
    r = _replace(client, owner, doc_id, "w1", "w4")
    assert r.status_code == 200, r.text
    # 旧请求已失效，故 open_request 为 None
    assert r.json()["open_request"] is None
    assert r.json()["witnesses"] == ["w4", "w2", "w3"]
    got = client.get(f"/documents/{doc_id}", headers=auth(owner)).json()
    assert got["status"] == "sealed" and "body" not in got

    records = client.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    actions = [x["action"] for x in records]
    assert "request_expired" in actions
    assert actions.index("request_expired") < actions.index("witness_replaced")

    # 新请求从 0 票开始，由新名单见证人投票
    req2 = _create_request(client, owner, doc_id).json()["request_id"]
    client.post(f"/open-requests/{req2}/approve", json={"digest": dg}, headers=auth(w4))
    r = client.post(f"/open-requests/{req2}/approve", json={"digest": dg}, headers=auth(w2))
    assert r.json()["status"] == "opened"


# ---------------------------------------------------------------------------
# 审计 / 多次替换 / 正文零泄露
# ---------------------------------------------------------------------------


def test_replace_audit_records(client):
    owner, (w1, w2, w3, w4), doc_id, dg = _setup(client)
    req = _create_request(client, owner, doc_id).json()["request_id"]
    client.post(f"/open-requests/{req}/approve", json={"digest": dg}, headers=auth(w1))

    assert _replace(client, owner, doc_id, "w1", "w4").status_code == 200
    records = client.get(f"/documents/{doc_id}/audit", headers=auth(owner)).json()["records"]
    last = records[-1]
    assert last["action"] == "witness_replaced"
    assert last["actor"] == "owner"
    assert "w1" in last["detail"] and "w4" in last["detail"]

    # waiting 因替换后票不足退回待同意时，应在 witness_replaced 前多一条
    # request_back_to_pending（同一事务内，序号连续）
    d2 = seal(client, owner, BODY + "3", ["w1", "w2", "w3"])
    doc2, dg2 = d2["document_id"], d2["digest"]
    rr = _create_request(client, owner, doc2, earliest_in=3600, deadline_in=7200).json()["request_id"]
    client.post(f"/open-requests/{rr}/approve", json={"digest": dg2}, headers=auth(w2))
    client.post(f"/open-requests/{rr}/approve", json={"digest": dg2}, headers=auth(w3))
    _replace(client, owner, doc2, "w2", "w4")
    actions = [x["action"] for x in
               client.get(f"/documents/{doc2}/audit", headers=auth(owner)).json()["records"]]
    assert actions[-2:] == ["request_back_to_pending", "witness_replaced"]


def test_chained_replacements_keep_three_distinct(client):
    owner, (w1, w2, w3, w4), doc_id, _ = _setup(client)
    register(client, "w5")
    assert _replace(client, owner, doc_id, "w1", "w4").status_code == 200
    # w1 已不在名单：用 w1 作为“新见证人”合法（已注册、非封存人、不在名单）
    r = _replace(client, owner, doc_id, "w2", "w1")
    assert r.status_code == 200
    assert r.json()["witnesses"] == ["w4", "w1", "w3"]
    # 旧 w2 失去权限
    assert client.get(f"/documents/{doc_id}", headers=auth(w2)).status_code == 403
    # 当前三人互异
    assert len(set(r.json()["witnesses"])) == 3


def test_replace_errors_never_leak_body(client):
    owner, (w1, w2, w3, w4), doc_id, _ = _setup(client)
    for resp in [
        _replace(client, owner, doc_id, "w1", "nobody"),       # 400 未注册
        _replace(client, owner, doc_id, "w9", "w4"),           # 400 不在名单
        _replace(client, w1, doc_id, "w2", "w4"),              # 403 非封存人
    ]:
        assert BODY not in resp.text, resp.text
