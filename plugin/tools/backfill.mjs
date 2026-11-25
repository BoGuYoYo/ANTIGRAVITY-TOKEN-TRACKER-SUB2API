#!/usr/bin/env node
// Backfill every local Antigravity conversation into sub2api's usage dashboard.
// One usage row per model call (PLANNER_RESPONSE), keyed on transcript line
// number, so repeats are counted as skipped instead of inserted.
//
//   node tools/backfill.mjs            # push everything
//   node tools/backfill.mjs --dry-run  # print what would be pushed

import {
  advanceSyncWatermarks,
  collectConversations,
  pushUsage,
  readRelayConfig,
} from '../lib/tokens.mjs';

const BATCH = 200;

const dryRun = process.argv.includes('--dry-run');
const cfg = readRelayConfig();

if (!cfg.enabled) {
  console.error('[backfill] relay 未启用：检查插件根目录的 relay.json（url / secret）');
  process.exit(1);
}

const conversations = collectConversations();
console.log(`[backfill] 本地会话 ${conversations.length} 个 → ${cfg.url}`);

let sent = 0;
let inserted = 0;
let skipped = 0;
let failed = 0;
let tokens = 0;

for (const conv of conversations) {
  const calls = (conv.calls || [])
    .filter((c) => (c.inp || 0) + (c.out || 0) + (c.cache || 0) > 0)
    .sort((a, b) => {
      const ca = String(a.sourceConvId || '');
      const cb = String(b.sourceConvId || '');
      if (ca !== cb) return ca.localeCompare(cb);
      return (a.line ?? 0) - (b.line ?? 0);
    });
  if (!calls.length) continue;

  const callTotal = calls.reduce((acc, c) => acc + (c.inp || 0) + (c.out || 0) + (c.cache || 0), 0);
  if (dryRun) {
    console.log(`  [dry] ${conv.id}  ${calls.length} 次调用  ${callTotal} tokens`);
    sent += calls.length;
    tokens += callTotal;
    continue;
  }

  for (let i = 0; i < calls.length; i += BATCH) {
    const batch = calls.slice(i, i + BATCH);
    const res = await pushUsage(conv.id, batch, { timeoutMs: 10000 });
    sent += batch.length;
    tokens += batch.reduce((acc, c) => acc + (c.inp || 0) + (c.out || 0) + (c.cache || 0), 0);
    if (res.ok) {
      inserted += res.data?.inserted ?? 0;
      skipped += res.data?.skipped ?? 0;
      const updates = {};
      for (const item of batch) {
        const cid = item.sourceConvId || conv.id;
        const lineNo = Number(item.line);
        if (cid && Number.isFinite(lineNo)) {
          updates[cid] = updates[cid] === undefined ? lineNo : Math.max(updates[cid], lineNo);
        }
      }
      advanceSyncWatermarks(updates);
    } else {
      failed += 1;
      console.error(`  ✗ ${conv.id}: ${res.reason || res.status}`);
      break;
    }
  }
}

console.log(
  `[backfill] ${dryRun ? '预演' : '完成'}：调用 ${sent} 次，tokens ${tokens}，` +
    `新写入 ${inserted}，已存在跳过 ${skipped}${failed ? `，失败批次 ${failed}` : ''}`,
);

if (failed && !inserted) process.exit(2);
