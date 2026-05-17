"""
FB first-comment dispatcher chạy trên GitHub Actions cron.

Đọc queue file: queue/.pending_fb_comments.json
Cho mỗi entry status=pending và due_at <= NOW UTC:
  - POST /post_id/comments với comment_text
  - Mark status=done + done_at
  - Hoặc retry nếu fail (max 5 attempts)

Cleanup: xóa entry done/failed > 3 ngày để giữ queue gọn.

Env vars cần (set qua GitHub Secrets):
  FB_PAGE_ACCESS_TOKEN — Page access token (long-lived)
"""
import os, sys, json, requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

QUEUE_FILE = Path("queue/.pending_fb_comments.json")
FB_GRAPH = "https://graph.facebook.com/v23.0"
MAX_ATTEMPTS = 5
MAX_AGE_DAYS = 3

TOKEN = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
if not TOKEN:
    print("[dispatch] ERR: FB_PAGE_ACCESS_TOKEN env var không có")
    sys.exit(1)


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def add_comment(post_id, text):
    try:
        r = requests.post(
            f"{FB_GRAPH}/{post_id}/comments",
            params={"access_token": TOKEN},
            data={"message": text},
            timeout=60,
        )
        if r.status_code == 200:
            return True, r.json().get("id", "?")
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


def main():
    if not QUEUE_FILE.exists():
        print(f"[dispatch] Queue file {QUEUE_FILE} không tồn tại — tạo empty")
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_FILE.write_text("[]", encoding="utf-8")
        return

    queue = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    if not queue:
        print("[dispatch] Queue rỗng, không có gì làm.")
        return

    now = now_utc()
    print(f"[dispatch] Queue có {len(queue)} entries — process tại NOW={now.isoformat()}")
    added = 0
    skipped = 0
    failed_now = 0

    for q in queue:
        status = q.get("status", "pending")
        if status != "pending":
            skipped += 1
            continue

        due = parse_iso(q.get("due_at"))
        if not due:
            q["status"] = "skipped"
            q["error"] = "Invalid due_at"
            skipped += 1
            continue

        if due > now:
            # Chưa đến giờ
            mins_left = (due - now).total_seconds() / 60
            print(f"  ⏳ {q['fb_post_id'][:20]}... due in {mins_left:.0f} min")
            continue

        # Đến giờ → thử add comment
        print(f"  → {q['fb_post_id'][:30]}... (due {(now - due).total_seconds() / 60:.0f} min ago)")
        ok, msg = add_comment(q["fb_post_id"], q["comment_text"])
        q["attempts"] = q.get("attempts", 0) + 1
        q["last_attempt"] = now.isoformat()
        q["last_error"] = None if ok else msg

        if ok:
            q["status"] = "done"
            q["done_at"] = now.isoformat()
            print(f"    ✓ Added comment_id={msg}")
            added += 1
        elif q["attempts"] >= MAX_ATTEMPTS:
            q["status"] = "failed"
            print(f"    ✗ Failed sau {MAX_ATTEMPTS} attempts: {msg}")
            failed_now += 1
        else:
            print(f"    ⏳ Will retry (attempt {q['attempts']}/{MAX_ATTEMPTS}): {msg}")
            failed_now += 1

    # Cleanup old done/failed entries
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    fresh = []
    for q in queue:
        if q.get("status") in ("done", "failed"):
            ts = parse_iso(q.get("done_at") or q.get("last_attempt") or q.get("queued_at"))
            if ts and ts < cutoff:
                continue
        fresh.append(q)

    QUEUE_FILE.write_text(json.dumps(fresh, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[dispatch] Xong: +{added} added, {failed_now} fail/retry, {skipped} skipped. Queue size: {len(fresh)}")


if __name__ == "__main__":
    main()
