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
EVENTS_TABLE    = os.getenv("EVENTS_TABLE", "LifeShot_Events")
SNS_TOPIC_ARN   = os.getenv("SNS_TOPIC_ARN")
CAMERA_ID       = "cam-01"

EXPECTED_COUNT  = int(os.getenv("EXPECTED_COUNT", "8"))
CONFIRM_FRAMES  = int(os.getenv("CONFIRM_FRAMES", "3"))

MIN_CONFIDENCE  = 75
MIN_BOX_AREA    = 0.005
MAX_BOX_AREA    = 0.60

MATCH_DIST      = 0.12  # מרחק נורמליזציה למציאת אדם זהה

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
        return resp("Not enough frames")

    frames = []
    for key in keys:
        people = detect_people(bucket=FRAMES_BUCKET, key=key)
        frames.append({
            "key": key,
            "people": people,
            "count": len(people)
        })

    missing_streak = 0

    for i in range(1, len(frames)):
        prev = frames[i - 1]
        curr = frames[i]

        if curr["count"] == EXPECTED_COUNT - 1:
            missing_streak += 1
        else:
            missing_streak = 0

        if missing_streak >= CONFIRM_FRAMES:
            missing_person = find_missing_person(
                prev["people"], curr["people"]
            )

            if not missing_person:
                continue

            alert_image_key = draw_alert_box(
                bucket=FRAMES_BUCKET,
                source_key=prev["key"],
                person=missing_person
            )

            save_event(
                frame_key=prev["key"],
                alert_image=alert_image_key,
                expected=EXPECTED_COUNT,
                detected=curr["count"]
            )

            return resp({
                "status": "ALERT_SENT",
                "alert_frame": prev["key"],
                "alert_image": f"s3://{FRAMES_BUCKET}/{alert_image_key}",
                "expected": EXPECTED_COUNT,
                "detected": curr["count"]
            })

    return resp({
        "status": "NO_ALERT",
        "expected": EXPECTED_COUNT,
        "frames": [
            {"key": f["key"], "count": f["count"]} for f in frames
        ]
    })

# =========================
# Rekognition
# =========================
def detect_people(bucket, key):
    res = rekognition.detect_labels(
        Image={"S3Object": {"Bucket": bucket, "Name": key}},
        MaxLabels=20,
        MinConfidence=MIN_CONFIDENCE
    )

    people = []

    for label in res["Labels"]:
        if label["Name"] == "Person":
            for inst in label.get("Instances", []):
                box = inst["BoundingBox"]
                area = box["Width"] * box["Height"]

                if not (MIN_BOX_AREA <= area <= MAX_BOX_AREA):
                    continue

                cx = box["Left"] + box["Width"] / 2
                cy = box["Top"]  + box["Height"] / 2

                people.append({
                    "box": box,
                    "center": (cx, cy)
                })

    return people

# =========================
# Find missing person
# =========================
def find_missing_person(prev_people, curr_people):
    for p in prev_people:
        found = False
        for c in curr_people:
            dx = p["center"][0] - c["center"][0]
            dy = p["center"][1] - c["center"][1]
            dist = (dx*dx + dy*dy) ** 0.5
            if dist < MATCH_DIST:
                found = True
                break
        if not found:
            return p
    return None

# =========================
# Draw alert box
# =========================
def draw_alert_box(bucket, source_key, person):
    obj = s3.get_object(Bucket=bucket, Key=source_key)
    img = Image.open(io.BytesIO(obj["Body"].read()))
    draw = ImageDraw.Draw(img)

    w, h = img.size
    box = person["box"]

    left   = box["Left"] * w
    top    = box["Top"] * h
    right  = left + box["Width"] * w
    bottom = top  + box["Height"] * h

    draw.rectangle([left, top, right, bottom], outline="red", width=6)
    draw.text((left, top - 10), "LAST SEEN", fill="red")

    alert_key = source_key.replace(".png", "_ALERT.png")

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

# =========================
# Save event + notify
# =========================
def save_event(frame_key, alert_image, expected, detected):
    now = int(time.time())
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    event = {
        "eventId": f"EVT-{now}",
        "timestamp": str(now),
        "camera_id": CAMERA_ID,
        "status": "OPEN",
        "frame_key": frame_key,
        "alert_image": alert_image,
        "expected": expected,
        "detected": detected,
        "created_at": now_str
    }

    dynamodb.Table(EVENTS_TABLE).put_item(Item=event)

    if SNS_TOPIC_ARN:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="🚨 LIFESHOT DROWNING ALERT",
            Message=(
                f"Drowning suspected\n"
                f"Camera: {CAMERA_ID}\n"
                f"Last seen: s3://{FRAMES_BUCKET}/{frame_key}\n"
                f"Marked image: s3://{FRAMES_BUCKET}/{alert_image}"
            )
        )

# =========================
# Helpers
# =========================
def list_frames_numeric(bucket, prefix):
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    frames = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = key.split("/")[-1]
            m = re.match(r"(\d+)\.(png|jpg|jpeg)", name.lower())
            if m:
                frames.append((int(m.group(1)), key))

    frames.sort(key=lambda x: x[0])
    return [k for (_, k) in frames]

def resp(body):
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False)
    }
