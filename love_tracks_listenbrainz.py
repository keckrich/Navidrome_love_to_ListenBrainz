import sqlite3
import requests
import time
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

# --------------------------------------------------
# CONFIG FROM ENVIRONMENT
# --------------------------------------------------
DB_PATH = os.environ.get("NAVIDROME_DB_PATH", "/data/navidrome.db")
LISTENBRAINZ_TOKEN = os.environ.get("LISTENBRAINZ_TOKEN", "")
LISTENBRAINZ_USER = os.environ.get("LISTENBRAINZ_USER", "")
CRON_SCHEDULE = os.environ.get("CRON_SCHEDULE", "").strip()
RUN_ON_START = os.environ.get("RUN_ON_START", "false").lower() in ("true", "1", "yes")
SCHEDULER_TZ = os.environ.get("TZ", "UTC")

# Set after each successful sync; drives incremental mode on cron ticks.
# None → full diff sync. Resets to None on container restart (intentional).
last_run_time: datetime | None = None

FEEDBACK_SUBMIT_URL = "https://api.listenbrainz.org/1/feedback/recording-feedback"
FEEDBACK_GET_URL = "https://api.listenbrainz.org/1/feedback/user/{user}/get-feedback"
REQUEST_DELAY = 0.5
MAX_RETRIES = 6
RETRY_DELAY = 1.5
LB_PAGE_SIZE = 1000

# --------------------------------------------------
# LISTENBRAINZ: FETCH ALL LOVED MBIDs
# --------------------------------------------------
def fetch_loved_mbids(user, token):
    url = FEEDBACK_GET_URL.format(user=user)
    headers = {"Authorization": f"Token {token}"}
    loved = set()
    offset = 0

    print(f"Fetching loved tracks from ListenBrainz for user: {user}")

    while True:
        params = {"score": 1, "count": LB_PAGE_SIZE, "offset": offset}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        feedback = data.get("feedback", [])
        for item in feedback:
            mbid = item.get("recording_mbid")
            if mbid:
                loved.add(mbid)

        print(f"  Fetched {offset + len(feedback)} loved tracks so far...")

        if len(feedback) < LB_PAGE_SIZE:
            break
        offset += LB_PAGE_SIZE
        time.sleep(0.2)

    print(f"Total loved in ListenBrainz: {len(loved)}")
    return loved

# --------------------------------------------------
# NAVIDROME: QUERIES
# --------------------------------------------------
def _db_connect(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

def query_all_starred(db_path):
    conn = _db_connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT mf.mbz_recording_id, mf.artist, mf.title, mf.album, a.starred_at
        FROM media_file mf
        JOIN annotation a ON a.item_id = mf.id
        WHERE a.starred = TRUE
          AND mf.mbz_recording_id IS NOT NULL
          AND TRIM(mf.mbz_recording_id) != ''
        ORDER BY a.starred_at
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

def count_starred_no_mbid(db_path):
    conn = _db_connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*)
        FROM media_file mf
        JOIN annotation a ON a.item_id = mf.id
        WHERE a.starred = TRUE
          AND (mf.mbz_recording_id IS NULL OR TRIM(mf.mbz_recording_id) = '')
    """)
    count = cursor.fetchone()[0]
    conn.close()
    return count

def query_starred_since(db_path, since: datetime):
    since_str = since.strftime("%Y-%m-%d %H:%M:%S")
    conn = _db_connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT mf.mbz_recording_id, mf.artist, mf.title, mf.album, a.starred_at
        FROM media_file mf
        JOIN annotation a ON a.item_id = mf.id
        WHERE a.starred = TRUE
          AND mf.mbz_recording_id IS NOT NULL
          AND TRIM(mf.mbz_recording_id) != ''
          AND a.starred_at IS NOT NULL
          AND a.starred_at > ?
        ORDER BY a.starred_at
    """, (since_str,))
    rows = cursor.fetchall()
    conn.close()
    return rows

# --------------------------------------------------
# LISTENBRAINZ: SUBMIT
# --------------------------------------------------
def submit_loved_tracks(tracks, token):
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }

    loved = 0
    failed = 0
    failed_list = []

    for recording_mbid, artist, title, album, starred_at in tracks:
        payload = {"recording_mbid": recording_mbid, "score": 1}

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    FEEDBACK_SUBMIT_URL,
                    headers=headers,
                    json=payload,
                    timeout=10,
                )

                if response.status_code == 200:
                    loved += 1
                    print(f"❤️  Loved: {artist} – {title}")
                    break
                else:
                    failed += 1
                    msg = f"❌ Failed: {artist} – {title} | Status: {response.status_code} | {response.text}"
                    print(msg)
                    failed_list.append(msg)
                    break

            except Exception as e:
                if attempt < MAX_RETRIES:
                    print(f"⚠️  Attempt {attempt} failed for {artist} – {title}, retrying in {RETRY_DELAY}s... | {e}")
                    time.sleep(RETRY_DELAY)
                else:
                    failed += 1
                    msg = f"❌ Error: {artist} – {title} | {e}"
                    print(msg)
                    failed_list.append(msg)

        time.sleep(REQUEST_DELAY)

    print("\n---------------- SUMMARY ----------------")
    print(f"❤️  Newly loved submitted : {loved}")
    print(f"❌ Failed                : {failed}")
    if failed_list:
        print("\nFailed tracks:")
        for msg in failed_list:
            print(f"  {msg}")

    return loved, failed

# --------------------------------------------------
# SYNC MODES
# --------------------------------------------------
def full_sync():
    """Fetch full LB loved set, diff against all Navidrome starred, push delta."""
    print("Mode: full diff sync")
    lb_loved = fetch_loved_mbids(LISTENBRAINZ_USER, LISTENBRAINZ_TOKEN)

    all_starred = query_all_starred(DB_PATH)
    no_mbid_count = count_starred_no_mbid(DB_PATH)
    print(f"Navidrome: {len(all_starred)} starred with MBIDs, {no_mbid_count} without.")

    to_submit = [t for t in all_starred if t[0] not in lb_loved]
    print(f"Already in LB: {len(all_starred) - len(to_submit)} | New to submit: {len(to_submit)}")

    if to_submit:
        submit_loved_tracks(to_submit, LISTENBRAINZ_TOKEN)
    else:
        print("Nothing to sync. All starred tracks are already loved in ListenBrainz. ✅")

def incremental_sync(since: datetime):
    """Push only tracks starred since the last run — no LB prefetch needed."""
    print(f"Mode: incremental sync since {since.isoformat()}")
    new_tracks = query_starred_since(DB_PATH, since)
    print(f"Found {len(new_tracks)} newly starred track(s).")

    if new_tracks:
        submit_loved_tracks(new_tracks, LISTENBRAINZ_TOKEN)
    else:
        print("No new starred tracks since last run. ✅")

def sync():
    global last_run_time
    run_at = datetime.now(timezone.utc)
    print(f"\n{'='*52}")
    print(f"Sync started at {run_at.isoformat()}")
    print(f"{'='*52}")

    try:
        if last_run_time is not None:
            incremental_sync(last_run_time)
        else:
            full_sync()
        last_run_time = run_at
    except Exception as e:
        print(f"❌ Sync failed: {e}")
        raise

# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------
def main():
    missing = [v for v in ("LISTENBRAINZ_TOKEN", "LISTENBRAINZ_USER") if not os.environ.get(v)]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    if not Path(DB_PATH).exists():
        print(f"❌ Navidrome database not found at: {DB_PATH}")
        sys.exit(1)

    token_hint = LISTENBRAINZ_TOKEN[:4] + "****" if len(LISTENBRAINZ_TOKEN) > 4 else "****"
    print(f"{'='*52}")
    print(f"Navidrome → ListenBrainz sync starting")
    print(f"  DB path     : {DB_PATH}")
    print(f"  LB user     : {LISTENBRAINZ_USER}")
    print(f"  LB token    : {token_hint}")
    print(f"  Cron        : {CRON_SCHEDULE or '(none — run once and exit)'}")
    if CRON_SCHEDULE:
        print(f"  Timezone    : {SCHEDULER_TZ}")
        print(f"  RUN_ON_START: {RUN_ON_START}")
    print(f"{'='*52}")

    if not CRON_SCHEDULE:
        sync()
        return

    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print("❌ APScheduler not installed. Add 'apscheduler' to requirements.txt.")
        sys.exit(1)

    if RUN_ON_START:
        print("RUN_ON_START=true — running full sync immediately...")
        sync()

    scheduler = BlockingScheduler(timezone=SCHEDULER_TZ)
    trigger = CronTrigger.from_crontab(CRON_SCHEDULE)
    scheduler.add_job(sync, trigger)

    next_run = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    print(f"Scheduler running. Cron: '{CRON_SCHEDULE}' | Next run: {next_run.strftime('%Y-%m-%d %H:%M %Z')}")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("Scheduler stopped.")

if __name__ == "__main__":
    main()
