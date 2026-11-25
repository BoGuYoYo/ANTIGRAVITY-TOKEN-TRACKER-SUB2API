# Antigravity Token Tracker + sub2api Relay

将 Antigravity IDE 插件（基于 [Willian-Wu/Antigravity-Token-Tracker](https://github.com/Willian-Wu/Antigravity-Token-Tracker)）的本地 Token 统计反向上报接入到 `sub2api` 的 `usage_logs` 与后台仪表盘。

## 目录结构

- [`plugin/`](./plugin/)：Antigravity IDE 客户端插件（放置或软链到 `~/.gemini/config/plugins/token-tracker`）
  - 支持每次模型调用（`PLANNER_RESPONSE`）按 transcript 物理行号幂等上报
  - 支持 Checkpoint 轨迹重放去重、断网/超长会话断点续传（`sync-state.json`）与 Subagent/Swarm 全树聚合
  - 提供历史回填工具 [`plugin/tools/backfill.mjs`](./plugin/tools/backfill.mjs) 与 Sidecar 面板
- [`server/tracker-relay/`](./server/tracker-relay/)：部署在 `sub2api` 宿主机上的零依赖 Python HTTP 中继（`/opt/sub2api/tracker-relay`）
  - 使用 `INSERT ... ON CONFLICT (request_id, api_key_id) DO NOTHING` 原子幂等写入 `usage_logs`（cost=0，不参与计费与余额扣减）
  - 自动剥离推理档位后缀（`minimal/low/medium/high/xhigh`）并写入原生 `reasoning_effort` 字段
  - 插入历史或延迟记录时，自动同事务重算受影响的 `usage_dashboard_hourly` / `usage_dashboard_daily` 聚合表

## 快速配置

1. **服务端**：参考 [`server/tracker-relay/README.md`](./server/tracker-relay/README.md)，复制 `relay.env.example` 为 `relay.env` 并填入实际密钥与 ID，执行 `seed.sql`、配置 Caddy 路由并启动 `tracker-relay.service`。
2. **客户端**：复制 [`plugin/relay.example.json`](./plugin/relay.example.json) 为 `plugin/relay.json`（已列入 `.gitignore`），填入你的上报 URL 与 `secret` 后运行：
   ```bash
   node tools/backfill.mjs --dry-run
   node tools/backfill.mjs
   ```
