import boto3
import json
import os
import time
import re
import io
from datetime import datetime
from PIL import Image, ImageDraw


# =========================
# CONFIG
# =========================
FRAMES_BUCKET   = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")
FRAMES_PREFIX   = os.getenv("FRAMES_PREFIX", "LifeShot/")

EXPECTED_COUNT  = int(os.getenv("EXPECTED_COUNT", "8"))      # למשל 8 קבוע
CONFIRM_FRAMES  = int(os.getenv("CONFIRM_FRAMES", "1"))      # 1 כדי "לצלם הכל" (Debug)
MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "75"))

MIN_BOX_AREA    = float(os.getenv("MIN_BOX_AREA", "0.005"))
MAX_BOX_AREA    = float(os.getenv("MAX_BOX_AREA", "0.60"))

MATCH_DIST      = float(os.getenv("MATCH_DIST", "0.12"))     # מרחק נורמליזציה התאמה בין אנשים

# Optional logging to Dynamo/SNS
EVENTS_TABLE    = os.getenv("EVENTS_TABLE", "")
SNS_TOPIC_ARN   = os.getenv("SNS_TOPIC_ARN", "")

rekognition = boto3.client("rekognition")
s3          = boto3.client("s3")
sns         = boto3.client("sns")
dynamodb    = boto3.resource("dynamodb")

# =========================
# Lambda Handler
# =========================
def lambda_handler(event, context):

    keys = list_frames_numeric(FRAMES_BUCKET, FRAMES_PREFIX)
    if len(keys) < 2:
        return _resp(200, {"status": "NO_FRAMES", "message": "Not enough frames found"})

    # Analyze each frame: detect people + boxes
    frames = []
    for key in keys:
        people = detect_people_with_boxes(FRAMES_BUCKET, key)
        frames.append({
            "key": key,
            "people": people,
            "count": len(people)
        })

    missing_streak = 0
    alerts = []

    # Iterate frame-by-frame
    for i in range(1, len(frames)):
        prev = frames[i - 1]
        curr = frames[i]

        # Condition: expected_count -> expected_count - 1 (missing one)
        if curr["count"] == EXPECTED_COUNT - 1:
            missing_streak += 1
        else:
            missing_streak = 0

        # When streak reaches confirm, we create an alert image
        if missing_streak >= CONFIRM_FRAMES:
            missing_person = find_missing_person(prev["people"], curr["people"], MATCH_DIST)

            if missing_person:
                alert_key = draw_missing_box(
                    bucket=FRAMES_BUCKET,
                    source_key=prev["key"],      # draw on last-seen frame (previous)
                    missing_person=missing_person
                )

                alert_item = {
                    "missing_detected_on_frame": curr["key"],
                    "last_seen_frame": prev["key"],
                    "expected_count": EXPECTED_COUNT,
                    "detected_count": curr["count"],
                    "alert_image_key": alert_key,
                    "alert_image_s3": f"s3://{FRAMES_BUCKET}/{alert_key}",
                    "missing_box": missing_person["box"],   # normalized 0..1 box
                    "missing_center": missing_person["center"]
                }
                alerts.append(alert_item)

                # OPTIONAL: write event to DynamoDB
                if EVENTS_TABLE:
                    save_event_to_dynamo(alert_item)

                # OPTIONAL: send SNS per alert (can be spammy if many!)
                if SNS_TOPIC_ARN:
                    send_sns(alert_item)

            # Important:
            # If CONFIRM_FRAMES=1, this will fire on every missing frame.
            # If CONFIRM_FRAMES>1, it fires once the streak threshold is reached.
            #
            # To avoid duplicate alerts on every subsequent frame in the same streak,
            # reset streak after capturing one alert:
            missing_streak = 0

    return _resp(200, {
        "status": "ALERTS_COLLECTED" if alerts else "NO_ALERT",
        "expected_count": EXPECTED_COUNT,
        "confirm_frames": CONFIRM_FRAMES,
        "total_frames": len(frames),
        "alerts_count": len(alerts),
        "alerts": alerts,
        "counts": [{"key": f["key"], "count": f["count"]} for f in frames]
    })

# =========================
# List frames by numeric order: 1.png,2.png,...
# =========================
def list_frames_numeric(bucket, prefix):
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    frames = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = key.split("/")[-1].lower()
            m = re.match(r"(\d+)\.(png|jpg|jpeg|webp)$", filename)
            if m:
                frames.append((int(m.group(1)), key))

    frames.sort(key=lambda x: x[0])
    return [k for (_, k) in frames]

# =========================
# Rekognition: people + boxes
# =========================
def detect_people_with_boxes(bucket, key):
    try:
        res = rekognition.detect_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MaxLabels=20,
            MinConfidence=MIN_CONFIDENCE
        )

        people = []
        for label in res.get("Labels", []):
            if label.get("Name") == "Person":
                for inst in label.get("Instances", []):
                    box = inst.get("BoundingBox", {})
                    w = float(box.get("Width", 0))
                    h = float(box.get("Height", 0))
                    area = w * h

                    if not (MIN_BOX_AREA <= area <= MAX_BOX_AREA):
                        continue

                    cx = float(box.get("Left", 0)) + w / 2.0
                    cy = float(box.get("Top", 0)) + h / 2.0

                    people.append({
                        "box": box,
                        "center": (cx, cy)
                    })

        return people
    except Exception as e:
        print(f"[ERROR] detect_people_with_boxes failed for {key}: {e}")
        return []

# =========================
# Find missing person between prev and curr
# =========================
def find_missing_person(prev_people, curr_people, max_dist):
    """
    Returns the first person from prev_people that has no close match in curr_people.
    Matching uses center distance in normalized coordinates (0..1).
    """
    for p in prev_people:
        found = False
        px, py = p["center"]

        for c in curr_people:
            cx, cy = c["center"]
            dx = px - cx
            dy = py - cy
            dist = (dx * dx + dy * dy) ** 0.5

            if dist < max_dist:
                found = True
                break

        if not found:
            return p

    return None

# =========================
# Draw red rectangle on "last seen" frame
# =========================
def draw_missing_box(bucket, source_key, missing_person):
    obj = s3.get_object(Bucket=bucket, Key=source_key)
    img_bytes = obj["Body"].read()

    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)

    W, H = img.size
    box = missing_person["box"]

    left   = float(box["Left"]) * W
    top    = float(box["Top"]) * H
    right  = left + float(box["Width"]) * W
    bottom = top  + float(box["Height"]) * H

    # Draw rectangle + label
    draw.rectangle([left, top, right, bottom], outline="red", width=6)

    label = "LAST SEEN"
    text_x = max(0, left)
    text_y = max(0, top - 20)
    draw.text((text_x, text_y), label, fill="red")

    # Save new key
    base, ext = _split_ext(source_key)
    alert_key = f"{base}_ALERT{ext}"

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    s3.put_object(
        Bucket=bucket,
        Key=alert_key,
        Body=buf,
        ContentType="image/png"
    )

    return alert_key

def _split_ext(key):
    if key.lower().endswith(".png"):
        return key[:-4], ".png"
    if key.lower().endswith(".jpg"):
        return key[:-4], ".jpg"
    if key.lower().endswith(".jpeg"):
        return key[:-5], ".jpeg"
    if key.lower().endswith(".webp"):
        return key[:-5], ".webp"
    return key, ".png"

# =========================
# Optional: DynamoDB + SNS
# =========================
def save_event_to_dynamo(alert_item):
    try:
        now_ts = int(time.time())
        now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        event_id = f"EVT-{now_ts}-{len(alert_item.get('last_seen_frame',''))}"

        item = {
            "eventId": event_id,
            "timestamp": str(now_ts),
            "created_at": now_str,
            "event_type": "MISSING_PERSON_FRAME",
            "expected_count": int(alert_item["expected_count"]),
            "detected_count": int(alert_item["detected_count"]),
            "missing_detected_on_frame": alert_item["missing_detected_on_frame"],
            "last_seen_frame": alert_item["last_seen_frame"],
            "alert_image_key": alert_item["alert_image_key"],
            "missing_box": json.dumps(alert_item["missing_box"]),
        }

        dynamodb.Table(EVENTS_TABLE).put_item(Item=item)
    except Exception as e:
        print(f"[WARN] Dynamo save failed: {e}")

def send_sns(alert_item):
    try:
        msg = (
            f"🚨 Missing person detected\n"
            f"Expected: {alert_item['expected_count']}\n"
            f"Detected: {alert_item['detected_count']}\n"
            f"Last seen frame: {alert_item['last_seen_frame']}\n"
            f"Detected missing on: {alert_item['missing_detected_on_frame']}\n"
            f"Marked image: {alert_item['alert_image_s3']}\n"
        )
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="⚠️ LIFESHOT - Missing Person Frames",
            Message=msg
        )
    except Exception as e:
        print(f"[WARN] SNS publish failed: {e}")

# =========================
# Response helper
# =========================
def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False)
    }
