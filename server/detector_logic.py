import boto3
import json
import os
import time
import re
from datetime import datetime
from collections import Counter

# =========================
# CONFIG
# =========================
SNS_TOPIC_ARN   = os.getenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:452845599848:LifeguardAlerts")
EVENTS_TABLE    = os.getenv("EVENTS_TABLE", "LifeShot_Events")

FRAMES_BUCKET   = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")
FRAMES_PREFIX   = os.getenv("FRAMES_PREFIX", "LifeShot/")
CAMERA_ID       = os.getenv("CAMERA_ID", "cam-01")

MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "75"))
MAX_FRAMES      = int(os.getenv("MAX_FRAMES", "17"))

CONFIRM_FRAMES  = int(os.getenv("CONFIRM_FRAMES", "2"))   # ❗ חשוב: 3+
EXPECTED_DROP   = 1                                      # 8 → 7

MIN_BOX_AREA    = float(os.getenv("MIN_BOX_AREA", "0.005"))
MAX_BOX_AREA    = float(os.getenv("MAX_BOX_AREA", "0.60"))

rekognition = boto3.client("rekognition")
s3          = boto3.client("s3")
sns         = boto3.client("sns")
dynamodb    = boto3.resource("dynamodb")

# =========================
# Lambda Handler
# =========================
def lambda_handler(event, context):

    keys = list_frames_numeric(FRAMES_BUCKET, FRAMES_PREFIX, MAX_FRAMES)
    if not keys:
        return _resp(200, "No frames found")

    # 1) Count people in each frame
    counts = []
    for key in keys:
        count = count_people_in_frame(FRAMES_BUCKET, key)
        counts.append({"key": key, "count": count})

    # 2) Expected baseline = most common count (mode)
    all_counts = [c["count"] for c in counts]
    baseline = Counter(all_counts).most_common(1)[0][0]

    # 3) Detect sustained missing person (8 → 7)
    drop_streak = 0
    alert_frame = None

    for item in counts:
        curr = item["count"]

        if curr == baseline - EXPECTED_DROP:
            drop_streak += 1
            alert_frame = item["key"]
        else:
            drop_streak = 0
            alert_frame = None

        if drop_streak >= CONFIRM_FRAMES:
            event_id = trigger_alert(
                CAMERA_ID,
                FRAMES_BUCKET,
                alert_frame,
                baseline,
                curr,
                counts
            )

            return _resp(200, {
                "status": "ALERT_SENT",
                "reason": "EXPECTED_PERSON_MISSING",
                "expected_count": baseline,
                "current_count": curr,
                "alert_frame": alert_frame,
                "s3_path": f"s3://{FRAMES_BUCKET}/{alert_frame}",
                "confirmFrames": CONFIRM_FRAMES,
                "counts": counts
            })

    return _resp(200, {
        "status": "NO_ALERT",
        "expected_count": baseline,
        "counts": counts
    })

# =========================
# List frames by numeric order
# =========================
def list_frames_numeric(bucket, prefix, max_frames):
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    frames = []

    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not _is_image(key):
                continue

            filename = key.split("/")[-1]
            match = re.match(r"(\d+)\.", filename)
            if not match:
                continue

            frames.append((int(match.group(1)), key))

    frames.sort(key=lambda x: x[0])
    frames = frames[:max_frames]

    return [k for (_, k) in frames]

def _is_image(key):
    k = key.lower()
    return k.endswith(".png") or k.endswith(".jpg") or k.endswith(".jpeg")

# =========================
# Rekognition counting
# =========================
def count_people_in_frame(bucket, key):
    try:
        res = rekognition.detect_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MaxLabels=20,
            MinConfidence=MIN_CONFIDENCE
        )

        count = 0
        for label in res.get("Labels", []):
            if label["Name"] == "Person":
                for inst in label.get("Instances", []):
                    box = inst.get("BoundingBox", {})
                    area = float(box.get("Width", 0)) * float(box.get("Height", 0))
                    if MIN_BOX_AREA <= area <= MAX_BOX_AREA:
                        count += 1

        return count

    except Exception as e:
        print(f"ERROR on {key}: {e}")
        return 0

# =========================
# Alert
# =========================
def trigger_alert(camera_id, bucket, key, baseline, curr_count, counts):
    now_ts = int(time.time())
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    event_id = f"EVT-{camera_id}-{now_ts}"

    dynamodb.Table(EVENTS_TABLE).put_item(Item={
        "eventId": event_id,
        "timestamp": str(now_ts),
        "camera_id": camera_id,
        "status": "OPEN",
        "alert_type": "EXPECTED_COUNT_MISSING",
        "frame_key": key,
        "expected_count": baseline,
        "current_count": curr_count,
        "counts_trace": json.dumps(counts)[:3500],
        "created_at": now_str
    })

    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject="🚨 LIFESHOT ALERT – Missing Swimmer",
        Message=(
            f"Possible drowning detected\n"
            f"Camera: {camera_id}\n"
            f"Frame: s3://{bucket}/{key}\n"
            f"Expected: {baseline}\n"
            f"Detected: {curr_count}\n"
            f"Time: {now_str}"
        )
    )

    return event_id

# =========================
# Response helper
# =========================
def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False)
    }
