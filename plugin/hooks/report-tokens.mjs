import { readFileSync } from 'node:fs';
import {
  advanceSyncWatermarks,
  collectConversationTreeForHook,
  filterPendingCalls,
  pushUsage,
  readSettings,
  readSyncState,
} from '../lib/tokens.mjs';

const HOOK_BATCH = 100;
const HOOK_SYNC_BUDGET_MS = 3500;

function fmt(n) {
  const num = Number(n) || 0;
  if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(2)}M`;
  if (num >= 10_000) return `${(num / 1_000).toFixed(1)}k`;
  return num.toLocaleString('en-US');
}

// Mirror unconfirmed model calls into sub2api's usage dashboard. Never let a
// relay failure break the hook: the chat summary must still render, and failed
// calls stay unwatermarked so the next invocation catches them up.
async function mirrorCalls(rootId, calls) {
  const list = Array.isArray(calls) ? calls : [];
  if (!list.length) return;
  try {
    const { watermarks } = readSyncState();
    // Order by (sourceConvId, line) so each batch advances per-transcript
    // watermarks monotonically without skipping earlier lines.
    const pending = filterPendingCalls(list, watermarks).sort((a, b) => {
      const ca = String(a.sourceConvId || '');
      const cb = String(b.sourceConvId || '');
      if (ca !== cb) return ca.localeCompare(cb);
      return (a.line ?? 0) - (b.line ?? 0);
    });
    if (!pending.length) return;

    const started = Date.now();
    for (let i = 0; i < pending.length; i += HOOK_BATCH) {
      if (i > 0 && Date.now() - started >= HOOK_SYNC_BUDGET_MS) break;
      const batch = pending.slice(i, i + HOOK_BATCH);
      const res = await pushUsage(rootId, batch);
      if (!res.ok) break;
      const updates = {};
      for (const item of batch) {
        const cid = item.sourceConvId || rootId;
        const lineNo = Number(item.line);
        if (cid && Number.isFinite(lineNo)) {
          updates[cid] = updates[cid] === undefined ? lineNo : Math.max(updates[cid], lineNo);
        }
      }
      advanceSyncWatermarks(updates);
    }
  } catch {
    /* relay is best-effort by design */
  }
}

async function main() {
  let payload = {};
  try {
    const raw = readFileSync(0, 'utf-8');
    if (raw.trim()) payload = JSON.parse(raw);
  } catch {
    process.stdout.write('{}');
    return;
  }

  const convId = payload.conversationId || 'current';
  const transcriptPath = payload.transcriptPath;
  const tree = collectConversationTreeForHook(convId, transcriptPath);
  const cur = tree.cur;
  const lastTurn = cur?.turns?.[cur.turns.length - 1] || null;
  const settings = readSettings();

  const parts = [];
  if (lastTurn) {
    parts.push(`本轮 ${fmt(lastTurn.totalTokens)} (In ${fmt(lastTurn.inputTokens)} / Out ${fmt(lastTurn.outputTokens)} / Cache ${fmt(lastTurn.cacheReadTokens)})`);
  } else if (cur) {
    parts.push(`本会话 ${fmt(cur.totalTokens)}`);
  } else {
    parts.push('Token 统计');
  }

  if (settings.hook.showSessionTotal && cur && lastTurn) {
    parts.push(`会话累计 ${fmt(tree.sessionTotalTokens)}`);
  }

  const md = `> 📊 **${parts[0]}**${parts.length > 1 ? ` · ${parts.slice(1).join(' · ')}` : ''} · [查看详情](sidecar://token-tracker/token_panel/)`;

  process.stdout.write(
    JSON.stringify({
      injectSteps: [
        {
          systemMessage: {
            systemMessage: '',
            metadata: {
              user_markdown: md,
            },
          },
        },
      ],
      terminationBehavior: '',
    }),
  );

  // Emitted after the summary so a slow relay can never delay the answer.
  await mirrorCalls(tree.rootId, tree.calls);
}

main().catch(() => {
  /* keep stdout clean for the hook protocol */
});
