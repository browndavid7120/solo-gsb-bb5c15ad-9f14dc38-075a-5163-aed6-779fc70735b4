#!/usr/bin/env python3
"""封存后替换见证人端到端示例（纯标准库）。

演示：
1. 封存后（资料未开封）封存人替换一名指定见证人；旧见证人立即失去访问/同意权限；
2. 进行中的开封请求：被替换者的同意失效、其他同意保留，有效票不足两票时
   请求退回待同意，新见证人凭封存摘要为本次请求投票即可开封；
3. 到最早开封时刻且两票已齐的请求会先开封并拒绝替换。

用法: python examples/demo_replace.py [base_url]
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def call(method: str, path: str, key: str | None = None, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **({"X-API-Key": key} if key else {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def iso(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def main() -> None:
    users = {}
    for name in ["r-alice", "r-bob", "r-carol", "r-dave", "r-eric"]:
        _, body = call("POST", "/register", payload={"username": name})
        users[name] = body["api_key"]
    owner = users["r-alice"]

    # ---------- 场景一：无进行中请求时直接替换 ----------
    text = "替换见证人示例正文：carol 已无法履职"
    _, sealed = call("POST", "/documents", owner,
                     {"body": text, "witnesses": ["r-bob", "r-carol", "r-dave"]})
    doc_id, digest = sealed["document_id"], sealed["digest"]

    code, resp = call("POST", f"/documents/{doc_id}/witness-replacements", owner,
                      {"replacee": "r-carol", "new_witness": "r-eric"})
    assert code == 200, resp
    print("replace (no active request):", json.dumps(resp, ensure_ascii=False))
    assert resp["witnesses"] == ["r-bob", "r-eric", "r-dave"]
    assert resp["open_request"] is None

    # 旧见证人 carol 立即失去访问权限
    code, _ = call("GET", f"/documents/{doc_id}", users["r-carol"])
    assert code == 403
    print("old witness r-carol access after replacement -> 403")

    # ---------- 场景二：进行中（waiting）请求，被替换者已投票 -> 退回待同意 ----------
    # 用最早开封时刻把请求保持在 waiting（两票在时刻前集齐）。
    text2 = "定时替换示例正文"
    _, sealed2 = call("POST", "/documents", owner,
                      {"body": text2, "witnesses": ["r-bob", "r-eric", "r-dave"]})
    doc2, dg2 = sealed2["document_id"], sealed2["digest"]
    _, req2 = call("POST", f"/documents/{doc2}/open-requests", owner,
                   {"reason": "会议到点再开", "deadline": iso(7200),
                    "earliest_open_at": iso(3600)})
    rid2 = req2["request_id"]
    call("POST", f"/open-requests/{rid2}/approve", users["r-bob"], {"digest": dg2})
    call("POST", f"/open-requests/{rid2}/approve", users["r-eric"], {"digest": dg2})
    _, info = call("GET", f"/documents/{doc2}", owner)
    assert info["latest_open_request"]["status"] == "waiting"

    # r-eric 无法履职 -> 替换为 r-carol（已注册、非封存人、当前不在名单）
    code, repl = call("POST", f"/documents/{doc2}/witness-replacements", owner,
                      {"replacee": "r-eric", "new_witness": "r-carol"})
    assert code == 200, repl
    print("replace voter in waiting:", json.dumps(repl, ensure_ascii=False))
    # r-eric 的同意失效，r-bob 的同意保留：有效票 1 < 2 -> 退回 pending
    assert repl["open_request"]["status"] == "pending"
    assert repl["open_request"]["approval_count"] == 1

    # 新见证人 r-carol 凭封存摘要为本次请求投票 -> 两票重新集齐 -> waiting
    _, voted = call("POST", f"/open-requests/{rid2}/approve", users["r-carol"],
                    {"digest": dg2})
    assert voted["status"] == "waiting", voted
    print("new witness voted on the SAME request:", json.dumps(voted, ensure_ascii=False))

    # 旧见证人 r-eric 已无权同意
    code, _ = call("POST", f"/open-requests/{rid2}/approve", users["r-eric"],
                   {"digest": dg2})
    assert code == 403
    print("old witness r-eric approval after replacement -> 403")

    # ---------- 场景三：到点两票已齐 -> 先开封并拒绝替换 ----------
    text3 = "到点优先示例正文"
    _, sealed3 = call("POST", "/documents", owner,
                      {"body": text3, "witnesses": ["r-bob", "r-carol", "r-dave"]})
    doc3, dg3 = sealed3["document_id"], sealed3["digest"]
    _, req3 = call("POST", f"/documents/{doc3}/open-requests", owner,
                   {"reason": "马上到点", "deadline": iso(3600),
                    "earliest_open_at": iso(1)})
    rid3 = req3["request_id"]
    call("POST", f"/open-requests/{rid3}/approve", users["r-bob"], {"digest": dg3})
    call("POST", f"/open-requests/{rid3}/approve", users["r-carol"], {"digest": dg3})
    time.sleep(1.2)  # 越过最早开封时刻（不主动扫描，由替换写事务确定性开封）

    code, _ = call("POST", f"/documents/{doc3}/witness-replacements", owner,
                   {"replacee": "r-dave", "new_witness": "r-eric"})
    assert code == 409
    _, got3 = call("GET", f"/documents/{doc3}", owner)
    assert got3["status"] == "opened" and got3["body"] == text3
    print("replace after earliest_open_at with 2 votes -> 409, document opened first")


if __name__ == "__main__":
    main()
