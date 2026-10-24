#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦麦积分清仓官 / McPoints Liquidator
基于麦当劳中国 MCP Server 的积分生命周期管理工具。

零第三方依赖，仅用 Python 标准库。
    python mcd_liquidator.py            # 启动本地服务，浏览器打开 http://127.0.0.1:8848
    python mcd_liquidator.py --scan     # 命令行模式，直接输出体检报告

Token 从环境变量 MCD_MCP_TOKEN 读取，永不硬编码。
"""

import json
import os
import re
import sys
import uuid
import urllib.request
import urllib.error
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MCP_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
DEFAULT_PORT = 8848

# 麦麦商城类目：(catRuleIds, spuCategory)  1=虚拟券 2=实物
# 默认列表会混入大量 point=0 的生日派对商品，按类目拉取才能拿到真正可兑换的
MALL_CATEGORIES = [("1>4", "1"), ("2", "2"), ("2>8", "2"), ("2>9", "2")]


# ---------------------------------------------------------------- MCP 客户端

class McpClient:
    """麦当劳 MCP Server 的 Streamable HTTP 客户端。"""

    def __init__(self, token):
        self.token = token
        self.session_id = None

    def _post(self, payload, is_notify=False):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(MCP_URL, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:400]
            except Exception:
                pass
            raise RuntimeError("MCP HTTP %s %s" % (e.code, body))
        except urllib.error.URLError as e:
            raise RuntimeError("MCP 网络错误: %s" % e.reason)
        if is_notify:
            return None
        return self._parse(raw)

    @staticmethod
    def _parse(raw):
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                return json.loads(raw)
            except ValueError:
                pass
        for line in raw.splitlines():          # SSE: data: {...}
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if not chunk:
                    continue
                try:
                    return json.loads(chunk)
                except ValueError:
                    continue
        return None

    def connect(self):
        self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-points-liquidator", "version": "1.0.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, is_notify=True)

    def call(self, name, arguments=None):
        resp = self._post({
            "jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        if not resp:
            return {"_raw": None}
        if "error" in resp:
            return {"_error": resp["error"]}
        return unwrap(resp.get("result", {}))


def unwrap(result):
    """还原 MCP 结果为 Python 对象。

    麦当劳 MCP 的 content[].text 是给大模型看的 Markdown 说明文档（以
    "# API Response Information" 开头），并非纯 JSON，直接 json.loads 必然失败。
    真正的结构化数据在 structuredContent.data 里，必须优先取它。
    """
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        if isinstance(sc.get("data"), (dict, list)):
            return sc["data"]
        return sc
    blocks = result.get("content") or []
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict)]
    joined = "\n".join(t for t in texts if t).strip()
    if not joined:
        return {"_raw": result, "_text": ""}
    try:
        parsed = json.loads(joined)
    except ValueError:
        return {"_raw": result, "_text": joined}
    if isinstance(parsed, dict) and isinstance(parsed.get("data"), (dict, list)):
        return parsed["data"]
    return parsed


# ------------------------------------------------------- 字段模糊提取（稳健）

def dig(obj, *keywords):
    """递归搜索第一个 key 命中关键词的数值，兼容官方字段命名差异。"""
    hits = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if any(w in kl for w in keywords) and isinstance(v, (int, float)):
                    hits.append((str(k), v))
                walk(v)
        elif isinstance(node, list):
            for item in node[:50]:
                walk(item)

    walk(obj)
    if not hits:
        return None
    score = []
    for k, v in hits:
        kl = k.lower()
        s = sum(3 for w in keywords if w in kl)
        if "expire" in kl or "expiring" in kl or "过期" in kl:
            s += 5
        score.append((s, v))
    score.sort(key=lambda x: -x[0])
    return score[0][1]


# 麦当劳 MCP 实际返回的字段名（小写化后精确匹配），模糊匹配作为兜底
EXP_NOW = ("currentmouthexpirepoint", "currentmonthexpirepoint", "expiringpoint")
EXP_NEXT = ("nextmouthexpirepoint", "nextmonthexpirepoint")
EXP_DONE = ("expiredpoint",)
PT_AVAIL = ("availablepoint", "currentpoint", "usablepoint")
PT_TOTAL = ("accumulativepoint", "totalpoint", "accumulatedpoint")
PT_FROZEN = ("frozenpoint",)
PT_USED = ("usedpoint",)


def find_num(obj, names):
    """递归精确匹配字段名（忽略大小写与下划线），返回第一个命中的数值。"""
    norm = [n.replace("_", "") for n in names]
    hits = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                kk = str(k).replace("_", "").lower()
                if kk in norm and isinstance(v, (int, float, str)):
                    try:
                        hits.append(float(v))
                    except (TypeError, ValueError):
                        pass
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for item in node[:50]:
                walk(item)

    walk(obj)
    return hits[0] if hits else None


def find_rows(obj, prefer_keys=("products", "items", "list", "records", "data")):
    """从任意结构的响应里挖出「商品行」列表。"""
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for k in prefer_keys:
            v = obj.get(k)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in obj.values():
            r = find_rows(v)
            if r:
                return r
    return []


def pick(row, *keywords):
    for k, v in row.items():
        kl = str(k).lower()
        if any(w in kl for w in keywords):
            return v
    return None


def as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ 业务核心

def scan(client):
    """体检：读积分账户 -> 拉商城可兑换商品 -> 生成清仓建议。"""
    account = client.call("query-my-account")
    products_raw = client.call("mall-points-products")

    def num(names, fuzzy):
        v = find_num(account, names)
        if v is None:
            v = dig(account, *fuzzy)
        return as_int(v) if v is not None else 0

    balance = num(PT_AVAIL, ("available", "可用", "point"))
    lifetime = num(PT_TOTAL, ("accum", "total", "累计"))
    frozen = num(PT_FROZEN, ("frozen", "冻结"))
    used = num(PT_USED, ("used", "已用"))
    exp_now = num(EXP_NOW, ("currentmouth", "expiring", "即将过期"))
    exp_next = num(EXP_NEXT, ("nextmouth",))
    exp_done = num(EXP_DONE, ("expired", "已过期"))

    expiring_pts = exp_now + exp_next
    risk = expiring_pts > 0

    goods, seen = [], set()

    def absorb(raw, category):
        for r in find_rows(raw):
            spu = pick(r, "spuid", "id", "code", "sku")
            key = str(spu)
            if not key or key in seen:
                continue
            cost = as_int(pick(r, "point", "points", "积分"))
            if cost <= 0:
                continue                       # 过滤 point=0 的派对商品
            seen.add(key)
            goods.append({
                "name": str(pick(r, "spuname", "name", "title", "productname") or "未命名商品"),
                "points": cost,
                "image": pick(r, "spuimage", "image", "picture", "img") or "",
                "spu_id": spu,
                "spu_category": category,
            })

    absorb(products_raw, "1")
    for rule, category in MALL_CATEGORIES:
        try:
            absorb(client.call("mall-points-products", {"catRuleIds": rule}), category)
        except Exception:
            continue
    goods.sort(key=lambda g: g["points"])

    pool = expiring_pts if risk else balance
    affordable = [g for g in goods if 0 < g["points"] <= pool]
    near = sorted(
        [g for g in goods if g["points"] > pool],
        key=lambda g: g["points"] - pool,
    )[:5]

    best = affordable[-1] if affordable else None          # 花掉最多积分、最划算
    cheapest = affordable[0] if affordable else None

    return {
        "balance": balance,
        "expiring": expiring_pts,
        "exp_now": exp_now,
        "exp_next": exp_next,
        "expired": exp_done,
        "used": used,
        "frozen": frozen,
        "lifetime": lifetime,
        "at_risk": risk,
        "pool": pool,
        "goods_count": len(goods),
        "affordable": affordable[:12],
        "near_miss": [{"name": g["name"], "points": g["points"],
                       "gap": g["points"] - pool} for g in near],
        "best_pick": best,
        "cheapest": cheapest,
        "raw_account_keys": sorted(account.keys())[:20] if isinstance(account, dict) else [],
    }


def redeem(client, spu_id, spu_category="1"):
    """积分兑换下单。

    官方文档要求 mall-create-order 的必填项是 skuId 与 spuCategory，
    skuId 需先由 mall-product-detail(spuId) 取得。
    """
    detail = client.call("mall-product-detail", {"spuId": spu_id})
    sku = pick(detail, "skuid", "sku", "skucode")
    if not sku:
        return {"_error": "未能从商品详情解析 skuId"}
    return client.call("mall-create-order", {
        "skuId": sku,
        "spuCategory": str(spu_category or "1"),
        "count": 1,
    })


# ------------------------------------------------------------ 演示数据兜底

MOCK_ACCOUNT = {"availablePoints": 2480, "expiringPoints": 900,
                "frozenPoints": 0, "totalPoints": 15320,
                "expiredPoints": 1860, "usedPoints": 4210}
MOCK_GOODS = [
    {"name": "麦旋风兑换券", "points": 400, "spu_id": "0", "spu_category": "1"},
    {"name": "中薯条兑换券", "points": 500},
    {"name": "板烧鸡腿堡兑换券", "points": 900},
    {"name": "麦辣鸡翅（2块）", "points": 1100},
    {"name": "巨无霸兑换券", "points": 1500},
    {"name": "麦当劳联名随行杯", "points": 3200},
]


def mock_scan():
    goods = sorted(MOCK_GOODS, key=lambda g: g["points"])
    pool = MOCK_ACCOUNT["expiringPoints"]
    affordable = [g for g in goods if 0 < g["points"] <= pool]
    near = sorted([g for g in goods if g["points"] > pool],
                  key=lambda g: g["points"] - pool)[:5]
    return {
        "balance": MOCK_ACCOUNT["availablePoints"],
        "expiring": pool,
        "exp_now": pool,
        "exp_next": 0,
        "expired": MOCK_ACCOUNT["expiredPoints"],
        "used": MOCK_ACCOUNT["usedPoints"],
        "frozen": 0,
        "lifetime": MOCK_ACCOUNT["totalPoints"],
        "at_risk": True,
        "pool": pool,
        "goods_count": len(goods),
        "affordable": affordable[:12],
        "near_miss": [{"name": g["name"], "points": g["points"],
                       "gap": g["points"] - pool} for g in near],
        "best_pick": affordable[-1] if affordable else None,
        "cheapest": affordable[0] if affordable else None,
        "raw_account_keys": [],
        "_mock": True,
    }


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    token = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html not found", "text/plain; charset=utf-8")
            return
        if path == "/api/scan":
            self._send(200, json.dumps(run_scan(self.token), ensure_ascii=False))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            payload = {}
        path = self.path.split("?")[0]
        if path == "/api/redeem":
            if not self.token:
                self._send(200, json.dumps(
                    {"ok": False, "msg": "未配置 MCD_MCP_TOKEN，演示模式不支持真实下单"},
                    ensure_ascii=False))
                return
            try:
                c = McpClient(self.token)
                c.connect()
                res = redeem(c, payload.get("spu_id", ""),
                             payload.get("spu_category", "1"))
                self._send(200, json.dumps({"ok": True, "result": res}, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
            return
        self._send(404, json.dumps({"error": "not found"}))


def run_scan(token):
    if not token:
        return mock_scan()
    try:
        c = McpClient(token)
        c.connect()
        return scan(c)
    except Exception as e:
        out = mock_scan()
        out["_error"] = str(e)
        return out


# -------------------------------------------------------------------- 入口

def cli_report(data):
    line = "-" * 52
    print(line)
    print("  麦麦积分体检报告")
    print(line)
    if data.get("_mock"):
        print("  [演示模式] 未检测到 MCD_MCP_TOKEN，以下为示例数据")
    if data.get("_error"):
        print("  [警告] MCP 调用失败: %s" % data["_error"])
    print("  可用积分      : %d" % data["balance"])
    print("  即将过期积分  : %d %s" % (data["expiring"], "<-- 有清仓风险!" if data["at_risk"] else ""))
    print("  已过期作废    : %d %s" % (data.get("expired", 0),
                                    "<-- 这些已经白白损失了" if data.get("expired", 0) > 0 else ""))
    print("  累计积分      : %d" % data["lifetime"])
    print(line)
    if data["at_risk"]:
        print("  过期前可兑 (%d 分内):" % data["pool"])
        for g in data["affordable"]:
            print("    - %-22s %d 分" % (g["name"], g["points"]))
        if data["best_pick"]:
            print("  最优解: %s (%d 分)" % (data["best_pick"]["name"], data["best_pick"]["points"]))
    print(line)
    if data["near_miss"]:
        print("  再攒一点点就能换:")
        for g in data["near_miss"]:
            print("    - %-22s %d 分 (还差 %d)" % (g["name"], g["points"], g["gap"]))
    print(line)


def main():
    ap = argparse.ArgumentParser(description="麦麦积分清仓官")
    ap.add_argument("--scan", action="store_true", help="命令行输出体检报告")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default=os.environ.get("MCD_MCP_TOKEN", ""))
    args = ap.parse_args()

    token = args.token.strip()

    if args.scan:
        cli_report(run_scan(token))
        return

    Handler.token = token
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("麦麦积分清仓官已启动: http://127.0.0.1:%d" % args.port)
    print("模式: %s" % ("真实 MCP 数据" if token else "演示模式（未设置 MCD_MCP_TOKEN）"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
