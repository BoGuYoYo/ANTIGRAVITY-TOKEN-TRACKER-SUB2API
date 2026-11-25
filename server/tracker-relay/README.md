# Antigravity Token Tracker → sub2api 反向上报

把 Antigravity IDE 插件 `Willian-Wu/Antigravity-Token-Tracker` 的本地 token 统计，
写进 sub2api 的 `usage_logs`，从而在 sub2api 后台的用量/仪表盘里看到同一批消耗。

## 数据流

```
Antigravity IDE
└─ token-tracker 插件 (hooks/report-tokens.mjs, 每轮 PostInvocation 触发)
     │  POST /tracker-ingest/ingest   X-Tracker-Secret: <secret>
     ▼
Caddy (your-sub2api-domain:8443)  handle /tracker-ingest/*  → strip_prefix
     ▼
tracker-relay.service  (127.0.0.1:8099, python3, /opt/sub2api/tracker-relay/relay.py)
     ▼  docker exec -i sub2api-postgres psql
PostgreSQL sub2api.usage_logs
     └─ 后台 dashboard 聚合服务 (interval=1m) 自动汇总进 usage_dashboard_hourly/daily
```

## 落点约定

| 对象 | 值 |
|---|---|
| accounts | id=34 `Antigravity IDE (Tracker)`，platform=antigravity，type=oauth，**schedulable=false**（永不参与上游调度） |
| api_keys | id=5 `antigravity-tracker-ingest`，user_id=2 |
| usage_logs | **每次模型调用一行**：`request_id = tracker-<source_conv_id>-l<transcript行号>`，`inbound_endpoint='tracker-ingest'`，`session_id=<根会话conversation_id>`，`billing_mode='token'` |
| 费用 | `input_cost/output_cost/total_cost/actual_cost` 全为 0，不参与计费、不动余额、不更新 api_keys.usage_* 限流窗口 |

幂等性：`usage_logs` 上存在 `UNIQUE(request_id, api_key_id)`，relay 使用 `INSERT ... ON CONFLICT (request_id, api_key_id) DO NOTHING` 原子去重；重复投递只会返回 `skipped`，不会重复计账。

当新插入记录的 `created_at` 早于 `usage_dashboard_aggregation_watermark.last_aggregated_at`（历史回填或网络中断后的补传）时，relay 会在同一事务内自动重算受影响小时与自然日的 `usage_dashboard_hourly` / `usage_dashboard_daily` 及活跃用户去重表，无需等后台任务或手工触发回填。

### 记录粒度：为什么是「每次调用」而不是「每轮」

`transcript.jsonl` 里每个 `PLANNER_RESPONSE` 是一次真实的模型调用。早期实现按**对话轮次**记一行，
结果是：同一轮内 Antigravity 会多次触发 hook，每次推的都是该轮的累计值，`request_id` 相同 →
除第一条外全部被幂等规则吃掉，表现为「只有第一次请求有记录」。

现在按调用记录，且幂等键用 **transcript 物理行号**而不是 `step_index`——实测 Antigravity 的
transcript 里回退重跑或 checkpoint 写入会让 `step_index` 重复。行号在同一文件内唯一且稳定；客户端同时在 `parseTranscript()` 中对完全相同的 `(step_index, created_at, model, inp, out, cache)` checkpoint 重放行去重。

| 场景 | request_id | session_id |
|---|---|---|
| 主会话第 42 行的调用 | `tracker-<main_conv>-l42` | `<main_conv>` |
| 子代理 `child_conv` 第 7 行的调用 | `tracker-<child_conv>-l7` | `<main_conv>` |
| 旧客户端（turn 摘要） | `tracker-<conv>-t<turn>` | `<conv>` |

客户端在 `~/.gemini/config/plugins/token-tracker/sync-state.json` 持久化每个 transcript 的已确认行号水位线，即使断网或单轮调用超过 30 次也能在下一次触发时完整补传。

## 模型名合并与推理档位

Antigravity 会按推理档位给出 `gemini-4-hydrogen-medium` / `-high` / `-xhigh`、`gemini-4-argon-high`、`claude-opus-5-5-low` 之类的变体。relay 默认自动剥离尾部 `-(minimal|low|medium|high|xhigh)` 档位（可通过 `TRACKER_AUTO_STRIP_EFFORT=false` 关闭），并按 `sub2api` 原生格式落库：

| 原始上报 | 落库 model / requested_model / upstream_model | reasoning_effort / requested_reasoning_effort | model_mapping_chain |
|---|---|---|---|
| `gemini-4-hydrogen-medium` | `gemini-4-hydrogen` | `medium` | `NULL` |
| `gemini-4-argon-high` | `gemini-4-argon` | `high` | `NULL` |
| `claude-opus-5-5-low` | `claude-opus-5-5` | `low` | `NULL` |
| `gemini-4-hydrogen` | `gemini-4-hydrogen` | `NULL` | `NULL` |

此外仍保留 `relay.env` 中的 `TRACKER_MODEL_MERGE_PREFIXES`（当前 `gemini-4-hydrogen,gemini-4-neon`）用于自定义前缀合并；命中自定义非档位前缀时会写入 `<原模型>→<合并模型>` 至 `model_mapping_chain`（与 `sub2api` 前端 `→` 分隔规则一致）。

## 文件清单

| 路径 | 作用 |
|---|---|
| `/opt/sub2api/tracker-relay/relay.py` | HTTP 接收端（零依赖，标准库 + `docker exec psql`，含仪表盘历史聚合同步） |
| `/opt/sub2api/tracker-relay/relay.env` | secret / 端口 / 落点 id（600 权限） |
| `/opt/sub2api/tracker-relay/seed.sql` | 幂等的账号 + api_key 初始化 SQL |
| `/opt/sub2api/tracker-relay/patch_caddy.py` | 给 Caddyfile 加 `/tracker-ingest/*` 路由（幂等，带备份） |
| `/etc/systemd/system/tracker-relay.service` | systemd 单元 |
| `~/.gemini/config/plugins/token-tracker/relay.json` | 客户端上报配置（url + secret + timeoutMs） |
| `~/.gemini/config/plugins/token-tracker/sync-state.json` | 客户端每会话已确认行号水位线（本地断点续传） |

## 客户端配置

`~/.gemini/config/plugins/token-tracker/relay.json`：

```json
{
  "enabled": true,
  "url": "https://your-sub2api-domain:8443/tracker-ingest/ingest",
  "secret": "<relay.env 里的 TRACKER_SECRET>",
  "timeoutMs": 2000,
  "model": ""
}
```

`url` 或 `secret` 为空、或 `enabled=false` 时，插件退回纯本地模式（行为与上游原版一致）。

## 运维命令

```bash
# 状态 / 日志
systemctl status tracker-relay
journalctl -u tracker-relay -n 50 --no-pager

# 重启（改 relay.py 或 relay.env 后）
sudo systemctl restart tracker-relay

# 手工灌一条（自测）
SECRET=$(sudo grep -oP '(?<=TRACKER_SECRET=).*' /opt/sub2api/tracker-relay/relay.env)
curl -s -X POST https://your-sub2api-domain:8443/tracker-ingest/ingest \
  -H "X-Tracker-Secret: $SECRET" -H 'Content-Type: application/json' \
  -d '{"conversation_id":"manual-check","turn":1,"model":"claude-sonnet-5","input_tokens":100,"output_tokens":20}'

# 查最近写入
docker exec sub2api-postgres psql -U sub2api -d sub2api -c \
  "select id,request_id,model,input_tokens,output_tokens,cache_read_tokens,created_at
   from usage_logs where api_key_id=5 order by id desc limit 10;"
```

## 回填历史

```bash
cd ~/.gemini/config/plugins/token-tracker
node tools/backfill.mjs --dry-run   # 先看会推多少
node tools/backfill.mjs             # 真推（重复执行安全）
```

## 排错

| 现象 | 位置 |
|---|---|
| 面板提示行正常但 sub2api 无记录 | `journalctl -u tracker-relay`：`bad_secret` / `db_error` |
| 返回 `202 {"inserted":0,"skipped":N}` | 该 turn 已存在（幂等命中），不是错误 |
| 返回 404 | Caddy 路由或 `uri strip_prefix` 未生效，`caddy validate` 后 reload |
| 返回 401 | `relay.json.secret` 与 `relay.env` 不一致 |
| 502 db_error | 检查 `docker exec sub2api-postgres psql -U sub2api -d sub2api -c 'select 1'` |

## 回滚

```bash
# 1) 停上报（客户端即时生效，无需动服务器）
#    ~/.gemini/config/plugins/token-tracker/relay.json 里把 enabled 改成 false

# 2) 删掉已写入的记录（若需清除仪表盘聚合残留，建议随后按受影响日期重算 usage_dashboard_daily/hourly）
docker exec sub2api-postgres psql -U sub2api -d sub2api -c "delete from usage_logs where api_key_id=5;"
# 同时清理本地 sync-state.json（如果以后会重新开启并回填）：
# rm -f ~/.gemini/config/plugins/token-tracker/sync-state.json

# 3) 移除服务与路由
sudo systemctl disable --now tracker-relay
sudo rm -f /etc/systemd/system/tracker-relay.service && sudo systemctl daemon-reload
cp /path/to/caddy/Caddyfile.bak.tracker-* /path/to/caddy/Caddyfile
/path/to/caddy/bin/caddy reload --config /path/to/caddy/Caddyfile
```

## 已知限制

- 子代理/旧会话的 transcript 里没有 `model` 字段时，行会记成 `antigravity-unknown`。
- 写入的是旁路记账数据：cost=0，不进入 `billing_usage_entries`，不影响用户余额与订阅扣费。
- 只统计 Antigravity IDE 本地 transcript 能看到的调用；绕过 IDE 直连上游的流量不在其中。
