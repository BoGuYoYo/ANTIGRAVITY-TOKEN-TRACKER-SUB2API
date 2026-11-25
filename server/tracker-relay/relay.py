#!/usr/bin/env python3
"""Antigravity Token Tracker -> sub2api usage_logs relay.

Zero-dependency HTTP ingest for the Antigravity-Token-Tracker plugin.
Receives per-call token stats from the IDE plugin and inserts them into
sub2api's usage_logs so the gateway dashboard shows IDE-side consumption.

Design notes:
- Written against sub2api 0.2.24 schema (usage_logs / api_keys / accounts).
- Inserts are cost-zero rows under a dedicated virtual account so billing and
  subscription balances are never touched.
- Idempotent by construction: usage_logs has UNIQUE(request_id, api_key_id),
  and request_id is derived from (source_conversation_id, transcript_line),
  so re-delivery is a no-op via ON CONFLICT DO NOTHING.
- Any newly inserted rows whose timestamps precede sub2api's aggregation
  watermark immediately refresh the affected hourly/daily dashboard tables in
  the same transaction so historical/delayed reports never disappear.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("TRACKER_SECRET", "")
PORT = int(os.environ.get("TRACKER_PORT", "8099"))
BIND = os.environ.get("TRACKER_BIND", "127.0.0.1")
PG_CONTAINER = os.environ.get("PG_CONTAINER", "sub2api-postgres")
PG_USER = os.environ.get("PG_USER", "sub2api")
PG_DB = os.environ.get("PG_DB", "sub2api")
USER_ID = int(os.environ["TRACKER_USER_ID"])
API_KEY_ID = int(os.environ["TRACKER_API_KEY_ID"])
ACCOUNT_ID = int(os.environ["TRACKER_ACCOUNT_ID"])
TZ_NAME = os.environ.get("TRACKER_TZ", "Asia/Shanghai").strip() or "Asia/Shanghai"
AUTO_STRIP_EFFORT = os.environ.get("TRACKER_AUTO_STRIP_EFFORT", "true").strip().lower() not in ("0", "false", "no")
MAX_BODY = 512 * 1024

CONV_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")
TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
TZ_RE = re.compile(r"^[A-Za-z0-9_/+.-]{1,64}$")
EFFORT_RE = re.compile(r"^(?P<base>.+)-(?P<effort>minimal|low|medium|high|xhigh)$")

if not TZ_RE.match(TZ_NAME):
    TZ_NAME = "Asia/Shanghai"

# Serialize SQL transactions touching shared dashboard summary buckets.
LOCK = threading.Lock()


def log(msg: str) -> None:
    sys.stderr.write("%s tracker-relay %s\n" % (datetime.now(timezone.utc).isoformat(), msg))
    sys.stderr.flush()


def as_int(value, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(lo, min(hi, n))


def clean_model(raw) -> str:
    if not isinstance(raw, str):
        return "antigravity-unknown"
    name = raw.strip()
    if not name or not MODEL_RE.match(name):
        return "antigravity-unknown"
    return name


# Optional explicit prefix merges in addition to automatic reasoning-effort stripping.
MERGE_PREFIXES = [
    item.strip()
    for item in os.environ.get("TRACKER_MODEL_MERGE_PREFIXES", "gemini-4-hydrogen,gemini-4-neon").split(",")
    if item.strip()
]


def normalize_model(raw_name: str) -> tuple[str, str | None, str | None]:
    """Return (base_model, reasoning_effort, mapping_chain)."""
    effort = None
    candidate = raw_name

    if AUTO_STRIP_EFFORT:
        m = EFFORT_RE.match(candidate)
        if m:
            candidate = m.group("base")
            effort = m.group("effort")

    for prefix in MERGE_PREFIXES:
        if candidate != prefix and candidate.startswith(prefix + "-"):
            return prefix, effort, "%s\u2192%s" % (raw_name, prefix)

    return candidate, effort, None


def clean_ts(raw):
    """Accept ISO-8601 or epoch seconds; fall back to server time (None)."""
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return None


def sql_str(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def make_request_id(
    source_conv: str,
    tag: str,
    line_val: int | None,
    step_val: int | None,
    turn: int,
) -> str:
    if len(source_conv) > 40:
        digest = hashlib.sha1(source_conv.encode("utf-8")).hexdigest()[:8]
        conv_key = "%s-%s" % (source_conv[:27], digest)
    else:
        conv_key = source_conv

    if line_val is not None:
        suffix = "l%d" % line_val
    elif step_val is not None:
        suffix = "s%d" % step_val
    else:
        suffix = "t%d" % turn

    if tag:
        rid = "tracker-%s-%s-%s" % (conv_key, tag, suffix)
    else:
        rid = "tracker-%s-%s" % (conv_key, suffix)

    if len(rid) > 64:
        # Preserve suffix uniqueness even if an unusual non-UUID conversation ID exceeds length.
        digest = hashlib.sha1(source_conv.encode("utf-8")).hexdigest()[:12]
        rid = "tracker-%s-%s" % (digest, suffix)
    return rid[:64]


def run_sql(sql: str) -> str:
    proc = subprocess.run(
        [
            "docker", "exec", "-i", PG_CONTAINER,
            "psql", "-U", PG_USER, "-d", PG_DB,
            "-v", "ON_ERROR_STOP=1", "-X", "-q", "-t", "-A", "-f", "-",
        ],
        input=sql.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()[:500])
    return proc.stdout.decode("utf-8", "replace")


def normalize_turn(payload: dict, index: int, default_conv: str = "") -> dict | None:
    session_conv = default_conv or payload.get("conversation_id") or payload.get("conversationId") or ""
    if not isinstance(session_conv, str) or not CONV_RE.match(session_conv):
        return None

    source_conv = (
        payload.get("source_conversation_id")
        or payload.get("sourceConversationId")
        or payload.get("conversation_id")
        or payload.get("conversationId")
        or session_conv
    )
    if not isinstance(source_conv, str) or not CONV_RE.match(source_conv):
        return None

    # A non-numeric turn must fall back to its batch position, otherwise every
    # such turn collapses to 0 and the dedupe key eats the extra rows.
    try:
        turn = int(payload.get("turn"))
    except (TypeError, ValueError):
        turn = index
    turn = max(0, min(1_000_000, turn))

    input_tokens = as_int(payload.get("input_tokens", payload.get("inputTokens")), 0, 2_000_000_000)
    output_tokens = as_int(payload.get("output_tokens", payload.get("outputTokens")), 0, 2_000_000_000)
    cache_read = as_int(payload.get("cache_read_tokens", payload.get("cacheReadTokens")), 0, 2_000_000_000)
    if input_tokens + output_tokens + cache_read == 0:
        return None

    raw_model = clean_model(payload.get("model"))
    model, effort, mapping_chain = normalize_model(raw_model)

    try:
        line_val = int(payload.get("line"))
    except (TypeError, ValueError):
        line_val = None
    if line_val is not None:
        line_val = max(0, min(100_000_000, line_val))

    raw_tag = payload.get("tag") if isinstance(payload.get("tag"), str) else ""
    raw_tag = raw_tag.strip()
    tag = raw_tag if TAG_RE.match(raw_tag) else ""
    # Prefer canonical source_conversation_id over parent+tag so hook and backfill match.
    if source_conv != session_conv:
        tag = ""

    step = payload.get("step")
    try:
        step_val = int(step)
    except (TypeError, ValueError):
        step_val = None
    step_val = None if step_val is None else max(0, min(10_000_000, step_val))

    request_id = make_request_id(source_conv, tag, line_val, step_val, turn)

    return {
        "request_id": request_id,
        "model": model,
        "reasoning_effort": effort,
        "mapping_chain": mapping_chain,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "session_id": session_conv[:255],
        "created_at": clean_ts(payload.get("ts") or payload.get("timestamp")),
    }


def build_sql(rows: list[dict]) -> str:
    values_sql = []
    for row in rows:
        created = "now()" if not row["created_at"] else "%s::timestamptz" % sql_str(row["created_at"])
        model_lit = sql_str(row["model"])
        effort_lit = sql_str(row["reasoning_effort"])
        chain_lit = sql_str(row["mapping_chain"])
        values_sql.append(
            "({user}, {key}, {acc}, {rid}, {model}, {model}, {model}, {effort}, {effort}, "
            "{inp}, {out}, {cache}, 0, 0, 0, 0, false, {created}, 0, 'token', 'tracker-ingest', "
            "{sid}, 1, {chain})".format(
                user=USER_ID,
                key=API_KEY_ID,
                acc=ACCOUNT_ID,
                rid=sql_str(row["request_id"]),
                model=model_lit,
                effort=effort_lit,
                inp=row["input_tokens"],
                out=row["output_tokens"],
                cache=row["cache_read_tokens"],
                created=created,
                sid=sql_str(row["session_id"]),
                chain=chain_lit,
            )
        )

    tz_lit = sql_str(TZ_NAME)
    return (
        "BEGIN;\n"
        "CREATE TEMP TABLE _tracker_ins (created_at timestamptz) ON COMMIT DROP;\n"
        "WITH ins AS (\n"
        "  INSERT INTO usage_logs (\n"
        "    user_id, api_key_id, account_id, request_id, model, requested_model, upstream_model,\n"
        "    reasoning_effort, requested_reasoning_effort,\n"
        "    input_tokens, output_tokens, cache_read_tokens,\n"
        "    input_cost, output_cost, total_cost, actual_cost,\n"
        "    stream, created_at, billing_type, billing_mode, inbound_endpoint,\n"
        "    session_id, rate_multiplier, model_mapping_chain\n"
        "  ) VALUES\n    " + ",\n    ".join(values_sql) + "\n"
        "  ON CONFLICT (request_id, api_key_id) DO NOTHING\n"
        "  RETURNING created_at\n"
        ")\n"
        "INSERT INTO _tracker_ins (created_at)\n"
        "SELECT created_at FROM ins;\n"
        "DO $$\n"
        "DECLARE\n"
        "  v_min timestamptz;\n"
        "  v_max timestamptz;\n"
        "  v_wm  timestamptz;\n"
        "  v_tz  text := " + tz_lit + ";\n"
        "  v_hour_start timestamptz;\n"
        "  v_hour_end   timestamptz;\n"
        "  v_day_start  date;\n"
        "  v_day_end    date;\n"
        "  v_day_hour_start timestamptz;\n"
        "  v_day_hour_end   timestamptz;\n"
        "BEGIN\n"
        "  SELECT MIN(created_at), MAX(created_at) INTO v_min, v_max FROM _tracker_ins;\n"
        "  IF v_min IS NULL THEN\n"
        "    RETURN;\n"
        "  END IF;\n"
        "  SELECT COALESCE(\n"
        "    (SELECT last_aggregated_at FROM usage_dashboard_aggregation_watermark WHERE id = 1),\n"
        "    '1970-01-01 00:00:00+00'::timestamptz\n"
        "  ) INTO v_wm;\n"
        "  IF v_min >= v_wm THEN\n"
        "    RETURN;\n"
        "  END IF;\n"
        "  v_hour_start := date_trunc('hour', v_min AT TIME ZONE v_tz) AT TIME ZONE v_tz;\n"
        "  v_hour_end := (date_trunc('hour', v_max AT TIME ZONE v_tz) + INTERVAL '1 hour') AT TIME ZONE v_tz;\n"
        "  v_day_start := (v_min AT TIME ZONE v_tz)::date;\n"
        "  v_day_end := (v_max AT TIME ZONE v_tz)::date + 1;\n"
        "  v_day_hour_start := v_day_start::timestamp AT TIME ZONE v_tz;\n"
        "  v_day_hour_end := v_day_end::timestamp AT TIME ZONE v_tz;\n"
        "  INSERT INTO usage_dashboard_hourly_users (bucket_start, user_id)\n"
        "  SELECT DISTINCT date_trunc('hour', created_at AT TIME ZONE v_tz) AT TIME ZONE v_tz, user_id\n"
        "  FROM usage_logs\n"
        "  WHERE created_at >= v_hour_start AND created_at < v_hour_end\n"
        "  ON CONFLICT DO NOTHING;\n"
        "  INSERT INTO usage_dashboard_daily_users (bucket_date, user_id)\n"
        "  SELECT DISTINCT (bucket_start AT TIME ZONE v_tz)::date, user_id\n"
        "  FROM usage_dashboard_hourly_users\n"
        "  WHERE bucket_start >= v_hour_start AND bucket_start < v_hour_end\n"
        "  ON CONFLICT DO NOTHING;\n"
        "  WITH hourly AS (\n"
        "    SELECT\n"
        "      date_trunc('hour', created_at AT TIME ZONE v_tz) AT TIME ZONE v_tz AS bucket_start,\n"
        "      COUNT(*) AS total_requests,\n"
        "      COALESCE(SUM(input_tokens), 0) AS input_tokens,\n"
        "      COALESCE(SUM(output_tokens), 0) AS output_tokens,\n"
        "      COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,\n"
        "      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,\n"
        "      COALESCE(SUM(total_cost), 0) AS total_cost,\n"
        "      COALESCE(SUM(actual_cost), 0) AS actual_cost,\n"
        "      COALESCE(SUM(COALESCE(account_stats_cost, total_cost) * COALESCE(account_rate_multiplier, 1)), 0) AS account_cost,\n"
        "      COALESCE(SUM(COALESCE(duration_ms, 0)), 0) AS total_duration_ms\n"
        "    FROM usage_logs\n"
        "    WHERE created_at >= v_hour_start AND created_at < v_hour_end\n"
        "    GROUP BY 1\n"
        "  ),\n"
        "  user_counts AS (\n"
        "    SELECT bucket_start, COUNT(*) AS active_users\n"
        "    FROM usage_dashboard_hourly_users\n"
        "    WHERE bucket_start >= v_hour_start AND bucket_start < v_hour_end\n"
        "    GROUP BY bucket_start\n"
        "  )\n"
        "  INSERT INTO usage_dashboard_hourly (\n"
        "    bucket_start, total_requests, input_tokens, output_tokens,\n"
        "    cache_creation_tokens, cache_read_tokens, total_cost, actual_cost,\n"
        "    account_cost, total_duration_ms, active_users, computed_at\n"
        "  )\n"
        "  SELECT\n"
        "    hourly.bucket_start, hourly.total_requests, hourly.input_tokens, hourly.output_tokens,\n"
        "    hourly.cache_creation_tokens, hourly.cache_read_tokens, hourly.total_cost, hourly.actual_cost,\n"
        "    hourly.account_cost, hourly.total_duration_ms, COALESCE(user_counts.active_users, 0), NOW()\n"
        "  FROM hourly\n"
        "  LEFT JOIN user_counts ON user_counts.bucket_start = hourly.bucket_start\n"
        "  ON CONFLICT (bucket_start) DO UPDATE SET\n"
        "    total_requests = EXCLUDED.total_requests,\n"
        "    input_tokens = EXCLUDED.input_tokens,\n"
        "    output_tokens = EXCLUDED.output_tokens,\n"
        "    cache_creation_tokens = EXCLUDED.cache_creation_tokens,\n"
        "    cache_read_tokens = EXCLUDED.cache_read_tokens,\n"
        "    total_cost = EXCLUDED.total_cost,\n"
        "    actual_cost = EXCLUDED.actual_cost,\n"
        "    account_cost = EXCLUDED.account_cost,\n"
        "    total_duration_ms = EXCLUDED.total_duration_ms,\n"
        "    active_users = EXCLUDED.active_users,\n"
        "    computed_at = EXCLUDED.computed_at;\n"
        "  WITH daily AS (\n"
        "    SELECT\n"
        "      (bucket_start AT TIME ZONE v_tz)::date AS bucket_date,\n"
        "      COALESCE(SUM(total_requests), 0) AS total_requests,\n"
        "      COALESCE(SUM(input_tokens), 0) AS input_tokens,\n"
        "      COALESCE(SUM(output_tokens), 0) AS output_tokens,\n"
        "      COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,\n"
        "      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,\n"
        "      COALESCE(SUM(total_cost), 0) AS total_cost,\n"
        "      COALESCE(SUM(actual_cost), 0) AS actual_cost,\n"
        "      COALESCE(SUM(account_cost), 0) AS account_cost,\n"
        "      COALESCE(SUM(total_duration_ms), 0) AS total_duration_ms\n"
        "    FROM usage_dashboard_hourly\n"
        "    WHERE bucket_start >= v_day_hour_start AND bucket_start < v_day_hour_end\n"
        "    GROUP BY (bucket_start AT TIME ZONE v_tz)::date\n"
        "  ),\n"
        "  user_counts AS (\n"
        "    SELECT bucket_date, COUNT(*) AS active_users\n"
        "    FROM usage_dashboard_daily_users\n"
        "    WHERE bucket_date >= v_day_start AND bucket_date < v_day_end\n"
        "    GROUP BY bucket_date\n"
        "  )\n"
        "  INSERT INTO usage_dashboard_daily (\n"
        "    bucket_date, total_requests, input_tokens, output_tokens,\n"
        "    cache_creation_tokens, cache_read_tokens, total_cost, actual_cost,\n"
        "    account_cost, total_duration_ms, active_users, computed_at\n"
        "  )\n"
        "  SELECT\n"
        "    daily.bucket_date, daily.total_requests, daily.input_tokens, daily.output_tokens,\n"
        "    daily.cache_creation_tokens, daily.cache_read_tokens, daily.total_cost, daily.actual_cost,\n"
        "    daily.account_cost, daily.total_duration_ms, COALESCE(user_counts.active_users, 0), NOW()\n"
        "  FROM daily\n"
        "  LEFT JOIN user_counts ON user_counts.bucket_date = daily.bucket_date\n"
        "  ON CONFLICT (bucket_date) DO UPDATE SET\n"
        "    total_requests = EXCLUDED.total_requests,\n"
        "    input_tokens = EXCLUDED.input_tokens,\n"
        "    output_tokens = EXCLUDED.output_tokens,\n"
        "    cache_creation_tokens = EXCLUDED.cache_creation_tokens,\n"
        "    cache_read_tokens = EXCLUDED.cache_read_tokens,\n"
        "    total_cost = EXCLUDED.total_cost,\n"
        "    actual_cost = EXCLUDED.actual_cost,\n"
        "    account_cost = EXCLUDED.account_cost,\n"
        "    total_duration_ms = EXCLUDED.total_duration_ms,\n"
        "    active_users = EXCLUDED.active_users,\n"
        "    computed_at = EXCLUDED.computed_at;\n"
        "END $$;\n"
        "SELECT COUNT(*) FROM _tracker_ins;\n"
        "COMMIT;"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "tracker-relay/1.1"

    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes | None:
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            buf = bytearray()
            while True:
                line = self.rfile.readline(64)
                if not line:
                    return None
                size_str = line.split(b";", 1)[0].strip()
                try:
                    chunk_len = int(size_str, 16)
                except ValueError:
                    return None
                if chunk_len == 0:
                    while self.rfile.readline(4096) not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(buf)
                if len(buf) + chunk_len > MAX_BODY:
                    return None
                data = self.rfile.read(chunk_len)
                if len(data) != chunk_len:
                    return None
                buf.extend(data)
                self.rfile.readline(4)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return None
        raw = self.rfile.read(length)
        return raw if len(raw) == length else None

    def log_message(self, fmt, *args):  # keep stdout quiet; use log()
        pass

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/health", "/healthz"):
            self._reply(200, {"status": "ok"})
            return
        self._reply(404, {"error": "not_found"})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/ingest", "/tracker-ingest/ingest", "/tracker-ingest"):
            self._reply(404, {"error": "not_found"})
            return

        token = self.headers.get("X-Tracker-Secret", "")
        if not SECRET or not hmac.compare_digest(token, SECRET):
            self._reply(401, {"error": "bad_secret"})
            return

        raw_body = self._read_body()
        if raw_body is None:
            self._reply(400, {"error": "bad_length"})
            return

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, {"error": "bad_json"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"error": "bad_payload"})
            return

        turns = payload.get("turns")
        if not isinstance(turns, list):
            turns = payload.get("calls")
        if not isinstance(turns, list):
            turns = [payload]

        envelope_conv = payload.get("conversation_id") or payload.get("conversationId") or ""

        rows = []
        seen_rids = set()
        for idx, item in enumerate(turns):
            if not isinstance(item, dict):
                continue
            row = normalize_turn(item, idx, envelope_conv)
            if row and row["request_id"] not in seen_rids:
                seen_rids.add(row["request_id"])
                rows.append(row)

        if not rows:
            self._reply(202, {"ok": True, "inserted": 0, "skipped": len(turns)})
            return

        try:
            with LOCK:
                out = run_sql(build_sql(rows))
        except Exception as exc:  # noqa: BLE001 - surface any psql failure to caller
            log("insert failed: %s" % exc)
            self._reply(502, {"error": "db_error", "detail": str(exc)})
            return

        lines = [line.strip() for line in out.splitlines() if line.strip()]
        try:
            inserted = int(lines[-1]) if lines else 0
        except ValueError:
            inserted = 0
        log("ingested %d/%d turn(s) from conversation %s"
            % (inserted, len(rows), rows[0]["session_id"]))
        self._reply(200, {"ok": True, "inserted": inserted, "skipped": len(rows) - inserted})


def main() -> None:
    if not SECRET:
        log("TRACKER_SECRET is empty; refusing to start")
        sys.exit(1)
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log("listening on %s:%d (account=%d key=%d user=%d)" % (BIND, PORT, ACCOUNT_ID, API_KEY_ID, USER_ID))
    server.serve_forever()


if __name__ == "__main__":
    main()
