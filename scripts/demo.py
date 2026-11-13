#!/usr/bin/env python3
"""组合泄露演示脚本（仅标准库）：对运行中的服务自动走一遍差分攻击。

用法：
  python scripts/demo.py                      # 默认 http://127.0.0.1:8000
  BASE_URL=http://host:port python scripts/demo.py
脚本会先调用 /api/reset 清空数据，再上传 sample/sample_groups.csv。
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
CSV_PATH = Path(__file__).resolve().parent.parent / "sample" / "sample_groups.csv"


def call(method: str, path: str, data=None, raw: bytes | None = None,
         content_type: str = "application/json"):
    url = BASE + path
    headers = {}
    body = None
    if raw is not None:
        body = raw
        headers["Content-Type"] = content_type
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), dict(e.headers)


def show(step: str, status: int, body: dict):
    print(f"\n--- {step}（HTTP {status}）---")
    if status == 200 and "publication" in body:
        p = body["publication"]
        print(f"通过核查，已发布合计 {p['aggregate']}，组：{'、'.join(p['groups'])}")
    elif "detail" in body:
        print(body["detail"])
    else:
        print(json.dumps(body, ensure_ascii=False)[:200])


def main() -> int:
    print(f"目标服务：{BASE}")

    status, body, _ = call("POST", "/api/reset")
    print("重置演示数据：", body)

    raw = CSV_PATH.read_bytes()
    boundary = "----rare-demo-boundary"
    multipart = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="sample_groups.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
    ).encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    status, body, _ = call(
        "POST", "/api/upload", raw=multipart,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    if status != 200:
        print("上传失败：", body)
        return 1
    total = body["base_table"]["total"]
    sensitive = [g["name"] for g in body["base_table"]["groups"] if g["sensitive"]]
    print(f"底表已固定：{len(body['base_table']['groups'])} 个组，公开总人数 {total}；"
          f"保密组（1~4 人）：{'、'.join(sensitive)}")

    # 第一步：含保密组的三组合计 22 —— 约束不足，应通过
    s1 = ["肺动脉高压", "溶酶体贮积症", "遗传性血管性水肿"]
    status, body, _ = call("POST", "/api/publish", {"groups": s1})
    show("第 1 次申请：肺高压+溶酶体+血管性水肿（真实合计 22）", status, body)
    if status != 200:
        print("首次安全发布未通过，演示终止。")
        return 1

    # 第二步：去掉保密组后的两组合计 19 —— 与第一次联立，22-19=3 直接暴露
    s2 = ["肺动脉高压", "遗传性血管性水肿"]
    status, body, headers = call("POST", "/api/publish", {"groups": s2})
    show("第 2 次申请：肺高压+血管性水肿（真实合计 19，与第 1 次相减即得 3 人组）",
         status, body)

    if status == 422:
        result_header = ""
        for key, value in headers.items():
            if key.lower() == "x-disclosure-result":
                result_header = value
        if result_header == "rejected":
            print("\n✔ 组合泄露被成功拦截，本次合计未发布，历史保持 1 条。")
            return 0
    print("\n✘ 预期的拒绝没有发生，请检查服务实现。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
