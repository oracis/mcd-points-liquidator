# MCP 接入说明

本项目基于**麦当劳中国官方 MCP Server** 构建，接入地址 `https://mcp.mcd.cn`，传输协议 **Streamable HTTP**，鉴权方式 `Authorization: Bearer <MCP_TOKEN>`。

Token 申请：<https://open.mcd.cn/mcp>（手机号登录后，控制台 → 激活 → 复制 Token）。

## 实际调用的 Tool

| # | Tool 名称 | 在本项目中的作用 | 业务价值 |
|---|---|---|---|
| 1 | `query-my-account` | 读取积分账户：可用积分、累计积分、冻结积分、**即将过期积分** | 唯一的「时间压力」数据源，是整个项目的触发点 |
| 2 | `mall-points-products` | 拉取麦麦商城可用积分兑换的商品列表（积分价、图片、商品 ID） | 建立兑换候选池 |
| 3 | `mall-product-detail` | 查询单个商品的详情与有效期 | 兑换前确认，避免误兑 |
| 4 | `mall-create-order` | 积分兑换下单，返回订单号与券码 | 完成「发现 → 决策 → 兑换」闭环 |
| 5 | `query-my-coupons` | 查询账户下已有优惠券 | 与积分叠加使用的辅助判断 |

## 调用流程

```
用户启动
   │
   ▼
[1] query-my-account ────► 解析 availablePoints / expiringPoints
   │                        │
   │                        ├─ expiringPoints == 0 ─► 提示「暂无过期风险」，降级为全额推荐
   │                        └─ expiringPoints  >  0 ─► 触发清仓预警
   ▼
[2] mall-points-products ──► 拉取商品池，按 point 升序排序
   │
   ▼
[3] 清仓决策
   │   • affordable : points <= 即将过期积分      → 「过期前还换得起」
   │   • best_pick  : affordable 中积分最高的那个  → 最优解（最大化消耗临期积分）
   │   • near_miss  : points > 池子，按差额升序取 5 → 「再攒一点点就能换」
   ▼
[4] 用户点「一键兑换」──► mall-create-order ──► 返回订单号 / 券码
   │
   ▼
[5] 生成「积分体检卡」图片，供分享传播
```

## 真实环境踩坑：结构化数据不在 `content[].text`

第一版代码解析 `content[].text`，跑真实账户时所有积分都读到 0 —— 一度以为是账号没积分，实测发现是取数方式错了。

麦当劳 MCP 的 `content[].text` 是一段**给大模型看的 Markdown 说明文档**，以 `# API Response Information` 开头，先罗列每个字段的含义，最后才附带完整响应体。`json.loads()` 对它必然失败。

真正的结构化数据在 **`structuredContent.data`**，标准封装为：

```
{success, code, message, datetime, traceId, data}
```

修正后必须优先取 `structuredContent`：

```python
sc = result.get("structuredContent")
if isinstance(sc, dict):
    return sc["data"] if isinstance(sc.get("data"), (dict, list)) else sc
```

真实账户返回的积分字段（`data` 内）：

| 字段 | 含义 |
|---|---|
| `availablePoint` | 可用积分 |
| `accumulativePoint` | 累计积分 |
| `currentMouthExpirePoint` | **本月将过期**积分 |
| `nextMouthExpirePoint` | 下月将过期积分 |
| `expiredPoint` | **已过期作废**积分 |
| `frozenPoint` / `usedPoint` | 冻结 / 已使用 |

注意 `expiredPoint`（已过期）和 `currentMouthExpirePoint`（即将过期）是两回事，必须分开读，否则会把沉没成本当成待清仓库存。项目里「已过期作废」单独做成主视觉 —— 实测这个数字往往比「即将过期」大得多，也更有冲击力。

## 字段兼容性处理

麦当劳 MCP 各 Tool 的返回结构存在版本差异，项目没有写死字段名，而是做了两层容错：

- `dig(obj, *keywords)`：递归遍历 JSON，按关键词模糊匹配数值字段（兼容 `expiringPoints` / `soonExpire` / `即将过期` 等不同命名）。
- `find_rows(obj)`：从任意嵌套结构中挖出商品行列表，兼容 `data` / `items` / `products` / `list` 等容器命名。

因此 MCP Server 后续升级字段命名时，本项目无需改动即可继续工作。

### 参数要点（依据官方文档与 `tools/list` 实测）

| Tool | 必填参数 | 说明 |
|---|---|---|
| `query-my-account` | 无 | 直接调用即可 |
| `mall-points-products` | `catRuleIds`（选填） | 类目筛选：商品券 `1>4`、实物 `2`、周边 `2>8`、实物礼品卡 `2>9`。**不传会混入大量 `point=0` 的生日派对商品** |
| `mall-product-detail` | `spuId` | 注意是 `spuId` 不是 `productId` |
| `mall-create-order` | `skuId`、`spuCategory` | **`skuId` 需先用 `mall-product-detail(spuId)` 换取**；`spuCategory`：`1` 虚拟 / `2` 实物 |

第一版代码把兑换写成 `{"productId": ...}`，参数名与必填项都不对，已按文档修正为 `skuId` + `spuCategory` 两步。

## 限流与错误处理

- 官方限制 **600 次/分钟**，本项目单次体检仅发起 2～5 次调用，不会触发 429。
- `401` 表示 Token 无效或过期，前端会回落到演示模式并给出提示，不会崩溃。
- MCP 响应可能是 JSON 也可能是 SSE（`text/event-stream`），客户端两种都做了解析。

## 业务价值

麦麦积分是**会过期**的沉默资产，官方 App 不会主动提醒用户「你有 900 分下个月清零」。本项目把这条隐性信息显性化，并直接给出「过期前最优兑换」的可执行结论，把「查积分 → 逛商城 → 心算比价 → 下单」四步压缩成一次点击。

## 接入 WorkBuddy

在 WorkBuddy 左侧栏【专家·技能·连接器】→【连接器】→【自定义连接器】→【配置 MCP】，填入：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

启用后，直接对话即可调用，例如：「查一下我的积分有没有快过期的，帮我换掉」。
