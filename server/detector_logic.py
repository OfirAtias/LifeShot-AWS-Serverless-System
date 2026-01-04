import boto3
import json
import os
import time
from datetime import datetime

# =========================
# CONFIG (Environment Vars recommended)
# =========================
SNS_TOPIC_ARN   = os.getenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:452845599848:LifeguardAlerts")
EVENTS_TABLE    = os.getenv("EVENTS_TABLE", "LifeShot_Events")

FRAMES_BUCKET   = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")           # חובה לשים (שם bucket)
FRAMES_PREFIX   = os.getenv("FRAMES_PREFIX", "LifeShot/")    # איפה הפריימים נמצאים
CAMERA_ID       = os.getenv("CAMERA_ID", "cam-01")

# Rekognition thresholds
MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "75"))  # רגישות לזיהוי Person
MAX_FRAMES      = int(os.getenv("MAX_FRAMES", "17"))        # כמה פריימים אחרונים לסרוק

# Logic thresholds
DROP_BY         = int(os.getenv("DROP_BY", "1"))            # ירידה ב-1 => חשוד
CONFIRM_FRAMES  = int(os.getenv("CONFIRM_FRAMES", "2"))     # כמה פריימים רצופים חייבים להיות ירידה כדי להתריע

# Filtering "bad person boxes" (heuristics)
MIN_BOX_AREA    = float(os.getenv("MIN_BOX_AREA", "0.005")) # מינימום שטח BoundingBox (0..1)
MAX_BOX_AREA    = float(os.getenv("MAX_BOX_AREA", "0.60"))  # מקסימום שטח

rekognition = boto3.client("rekognition")
s3          = boto3.client("s3")
sns         = boto3.client("sns")
dynamodb    = boto3.resource("dynamodb")

# =========================
# Lambda Handler
# =========================
def lambda_handler(event, context):
    """
    Trigger options:
    Runs scan over last frames in bucket/prefix.
    You can override 'prefix' and 'max_frames' via event payload.
    """
    if not FRAMES_BUCKET:
        return _resp(400, "Missing FRAMES_BUCKET env var")

    prefix = FRAMES_PREFIX
    max_frames = MAX_FRAMES

    if isinstance(event, dict):
        prefix = event.get("prefix", FRAMES_PREFIX)
        max_frames = int(event.get("max_frames", MAX_FRAMES))

    # 1) List frames
    keys = list_last_frames(FRAMES_BUCKET, prefix, max_frames)
    if not keys:
        return _resp(200, f"No frames found in s3://{FRAMES_BUCKET}/{prefix}")

    # 2) Process sequentially (Frame -> Frame)
    counts = []
    for k in keys:
        c = count_people_in_frame(FRAMES_BUCKET, k)
        counts.append({"key": k, "count": c})

    baseline = counts[0]["count"]

    # 3) Detect drop-by-1 (or more) with confirmation
    drop_streak = 0
    drop_frame_info = None

    for i in range(1, len(counts)):
        prev = counts[i - 1]["count"]
        curr = counts[i]["count"]

        if curr <= prev - DROP_BY:
            drop_streak += 1
            drop_frame_info = (counts[i]["key"], prev, curr)
        else:
            drop_streak = 0
            drop_frame_info = None

        if drop_streak >= CONFIRM_FRAMES:
            key, prev_count, curr_count = drop_frame_info

            event_id = trigger_alert(
                camera_id=CAMERA_ID,
                bucket=FRAMES_BUCKET,
                key=key,
                prev_count=prev_count,
                curr_count=curr_count,
                baseline=baseline,
                counts_trace=counts
            )

            # ✅ החזרה ברורה של שם הפריים הבעייתי
            return _resp(200, {
                "status": "ALERT_SENT",
                "eventId": event_id,
                "alert_frame": key,  # 👈 זה שם הפריים
                "s3_path": f"s3://{FRAMES_BUCKET}/{key}",
                "baseline": baseline,
                "prevCount": prev_count,
                "currCount": curr_count,
                "confirmFrames": CONFIRM_FRAMES,
                "counts": counts
            })

    return _resp(200, {
        "status": "NO_ALERT",
        "baseline": baseline,
        "confirmFrames": CONFIRM_FRAMES,
        "counts": counts
    })

# =========================
# Core: list frames
# =========================
def list_last_frames(bucket, prefix, max_frames):
    """
    Returns keys sorted ascending by LastModified (oldest->newest)
    so it's true "frame frame".
    """
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    objs = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not _is_image_key(key):
                continue
            objs.append({"Key": key, "LastModified": obj["LastModified"]})

    if not objs:
        return []

    objs.sort(key=lambda x: x["LastModified"])
    last = objs[-max_frames:] if max_frames > 0 else objs
    last.sort(key=lambda x: x["LastModified"])
    return [x["Key"] for x in last]

def _is_image_key(key):
    k = key.lower()
    return k.endswith(".jpg") or k.endswith(".jpeg") or k.endswith(".png") or k.endswith(".webp")

# =========================
# Core: count "heads"/people using Rekognition
# =========================
def count_people_in_frame(bucket, key):
    """
    Approximates "head count" by counting 'Person' instances from DetectLabels.
    Adds heuristic filtering to reduce false boxes (tiny/huge).
    """
    try:
        res = rekognition.detect_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MaxLabels=20,
            MinConfidence=MIN_CONFIDENCE
        )

        person_count = 0

        for label in res.get("Labels", []):
            if label.get("Name") == "Person":
                for inst in label.get("Instances", []):
                    box = inst.get("BoundingBox", {})
                    w = float(box.get("Width", 0))
                    h = float(box.get("Height", 0))
                    area = w * h

                    if area < MIN_BOX_AREA:
                        continue
                    if area > MAX_BOX_AREA:
                        continue

                    person_count += 1

        return person_count

    except Exception as e:
        print(f"[ERROR] count_people_in_frame failed for {key}: {str(e)}")
        return 0

# =========================
# Alerting + Event persistence
# =========================
def trigger_alert(camera_id, bucket, key, prev_count, curr_count, baseline, counts_trace):
    """
    Saves an event to DynamoDB and publishes SNS.
    """
    now_ts = int(time.time())
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    event_id = f"EVT-{camera_id}-{now_ts}"

    item = {
        "eventId": event_id,
        "timestamp": str(now_ts),
        "created_at": now_str,
        "camera_id": camera_id,
        "status": "OPEN",
        "alert_type": "MISSING_PERSON_DROP",
        "bucket": bucket,
        "frame_key": key,  # ✅ נשמר שם הפריים
        "prev_count": int(prev_count),
        "curr_count": int(curr_count),
        "baseline": int(baseline),
        "confirm_frames": int(CONFIRM_FRAMES),
        "drop_by": int(DROP_BY),
        "counts_trace": json.dumps(counts_trace)[0:3500]
    }

    dynamodb.Table(EVENTS_TABLE).put_item(Item=item)

    msg = (
        f"🚨 DROWNING SUSPECT (Count Drop)\n"
        f"Camera: {camera_id}\n"
        f"Prev Count: {prev_count}\n"
        f"Curr Count: {curr_count}\n"
        f"Frame: s3://{bucket}/{key}\n"  # ✅ שם הפריים בתוך ההודעה
        f"Time: {now_str}\n"
        f"Rule: drop_by={DROP_BY}, confirm_frames={CONFIRM_FRAMES}"
    )

    if SNS_TOPIC_ARN and "YOUR_SNS_ARN_HERE" not in SNS_TOPIC_ARN:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="⚠️ LIFESHOT EMERGENCY - Missing Person",
            Message=msg
        )

    return event_id

# =========================
# Helpers
# =========================
def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False)
    }
