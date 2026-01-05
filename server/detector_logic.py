import boto3
import json
import os
import re
import math
import io
import time
from datetime import datetime, timezone
from PIL import Image, ImageDraw
from botocore.exceptions import ClientError

# =========================
# ENV CONFIG
# =========================
BUCKET            = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")
FRAMES_PREFIX     = os.getenv("FRAMES_PREFIX", "LifeShot/")                 # input frames

TESTINGSET_PREFIX = os.getenv("TESTINGSET_PREFIX", "LifeShot/TestingSet/")  # ALL frames: green+red + OK/ALERT
WARNING_PREFIX    = os.getenv("WARNING_PREFIX", "LifeShot/Warning/")        # OPTIONAL: red-only (archive)

MAX_FRAMES        = int(os.getenv("MAX_FRAMES", "200"))
MIN_CONFIDENCE    = float(os.getenv("MIN_CONFIDENCE", "70"))

# Filter tiny/huge boxes (normalized area)
MIN_BOX_AREA      = float(os.getenv("MIN_BOX_AREA", "0.0015"))
MAX_BOX_AREA      = float(os.getenv("MAX_BOX_AREA", "0.70"))

# Matching params (used only to pick "where disappeared" AFTER counter drop)
MATCH_IOU_MIN      = float(os.getenv("MATCH_IOU_MIN", "0.08"))
MATCH_CENTER_MAX   = float(os.getenv("MATCH_CENTER_MAX", "0.12"))

PRESIGN_EXPIRES    = int(os.getenv("PRESIGN_EXPIRES", "3600"))  # seconds (default 1 hour)

# DynamoDB
EVENTS_TABLE_NAME  = os.getenv("EVENTS_TABLE_NAME", "LifeShot_Events")

# SNS
SNS_TOPIC_ARN      = os.getenv("SNS_TOPIC_ARN", "")  # set in Lambda env vars

rekognition = boto3.client("rekognition")
s3          = boto3.client("s3")
dynamodb    = boto3.resource("dynamodb")
events_table = dynamodb.Table(EVENTS_TABLE_NAME)
sns = boto3.client("sns")


# =========================
# Helpers
# =========================
def _resp(code, body_obj):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body_obj)}

def _is_image_key(key: str) -> bool:
    k = key.lower()
    return k.endswith(".png") or k.endswith(".jpg") or k.endswith(".jpeg")

def _basename(key: str) -> str:
    # LifeShot/4.png -> 4
    name = key.split("/")[-1]
    name = re.sub(r"\.(png|jpg|jpeg)$", "", name, flags=re.IGNORECASE)
    return name

def _center(b):
    return (
        float(b.get("Left", 0)) + float(b.get("Width", 0)) / 2.0,
        float(b.get("Top", 0)) + float(b.get("Height", 0)) / 2.0,
    )

def _dist(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)

def _iou(a, b):
    ax1, ay1 = float(a["Left"]), float(a["Top"])
    ax2, ay2 = ax1 + float(a["Width"]), ay1 + float(a["Height"])
    bx1, by1 = float(b["Left"]), float(b["Top"])
    bx2, by2 = bx1 + float(b["Width"]), by1 + float(b["Height"])

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0

    a_area = (ax2-ax1) * (ay2-ay1)
    b_area = (bx2-bx1) * (by2-by1)
    union = max(1e-9, a_area + b_area - inter)
    return inter / union

def _px(box, W, H):
    x1 = int(float(box["Left"]) * W)
    y1 = int(float(box["Top"]) * H)
    x2 = int((float(box["Left"]) + float(box["Width"])) * W)
    y2 = int((float(box["Top"]) + float(box["Height"])) * H)
    return x1, y1, x2, y2

def presign_get_url(bucket, key):
    if not key:
        return None
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_EXPIRES
        )
    except ClientError:
        return None

def publish_sns_alert(event_id, created_at_iso, prev_key, warn_key, prev_url, warn_url):
    if not SNS_TOPIC_ARN:
        print("[SNS] SNS_TOPIC_ARN is empty -> skipping publish")
        return

    subject = f"LifeShot ALERT: {event_id}"
    lines = [
        "🚨 POSSIBLE DROWNING DETECTED",
        "",
        f"EventId: {event_id}",
        f"CreatedAt: {created_at_iso}",
        "",
        "BEFORE (prev):",
        f"PrevImageKey: {prev_key or 'N/A'}",
        f"PrevImageUrl: {prev_url or 'N/A'}",
        "",
        "AFTER (alert):",
        f"WarningImageKey: {warn_key or 'N/A'}",
        f"WarningImageUrl: {warn_url or 'N/A'}",
        "",
        "Open your dashboard to view full details."
    ]
    msg = "\n".join(lines)

    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject,
            Message=msg
        )
        print(f"[SNS] Published alert for {event_id}")
    except Exception as e:
        print(f"[SNS ERROR] Failed to publish: {str(e)}")


# =========================
# Rekognition detection
# =========================
def detect_person_boxes(bucket, key):
    try:
        res = rekognition.detect_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MaxLabels=25,
            MinConfidence=MIN_CONFIDENCE
        )

        boxes = []
        for label in res.get("Labels", []):
            if label.get("Name") != "Person":
                continue

            for inst in label.get("Instances", []):
                box = inst.get("BoundingBox", {})
                w = float(box.get("Width", 0))
                h = float(box.get("Height", 0))
                area = w * h

                if area < MIN_BOX_AREA:
                    continue
                if area > MAX_BOX_AREA:
                    continue

                boxes.append({
                    "Left": float(box.get("Left", 0)),
                    "Top": float(box.get("Top", 0)),
                    "Width": w,
                    "Height": h,
                    "Conf": float(inst.get("Confidence", 0)),
                })

        return boxes

    except Exception as e:
        print(f"[ERROR] detect_person_boxes failed for {key}: {str(e)}")
        return []


# =========================
# Missing boxes ONLY when counter dropped
# =========================
def find_missing_boxes(prev_boxes, curr_boxes):
    if not prev_boxes:
        return []
    if not curr_boxes:
        return prev_boxes[:]

    missing = []
    for pb in prev_boxes:
        best_iou = 0.0
        best_dist = 999.0
        pc = _center(pb)

        for cb in curr_boxes:
            iou = _iou(pb, cb)
            dist = _dist(pc, _center(cb))
            if iou > best_iou:
                best_iou = iou
            if dist < best_dist:
                best_dist = dist

        matched = (best_iou >= MATCH_IOU_MIN) or (best_dist <= MATCH_CENTER_MAX)
        if not matched:
            missing.append(pb)

    return missing


def pick_top_missing(prev_boxes, curr_boxes, missing_candidates, drop_by):
    if drop_by <= 0:
        return []
    if not missing_candidates:
        return []
    if len(missing_candidates) <= drop_by:
        return missing_candidates

    scored = []
    for pb in missing_candidates:
        best_iou = 0.0
        best_dist = 999.0
        pc = _center(pb)

        for cb in (curr_boxes or []):
            iou = _iou(pb, cb)
            dist = _dist(pc, _center(cb))
            if iou > best_iou:
                best_iou = iou
            if dist < best_dist:
                best_dist = dist

        score = (1.0 - best_iou) + best_dist  # worse = higher
        scored.append((score, pb))

    scored.sort(reverse=True, key=lambda x: x[0])
    return [pb for _, pb in scored[:drop_by]]


# =========================
# Rendering + S3 output
# =========================
def render_png(src_bucket, src_key, title, curr_boxes, missing_boxes, draw_green, draw_red):
    img_bytes = s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # Title bar
    draw.rectangle([0, 0, W, 58], fill=(0, 0, 0))
    draw.text((12, 18), title, fill=(255, 255, 255))

    # GREEN: current detections + label PERSON
    if draw_green:
        for b in curr_boxes:
            x1, y1, x2, y2 = _px(b, W, H)
            draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=4)
            draw.text((x1 + 6, max(62, y1 - 18)), "PERSON", fill=(0, 255, 0))

    # RED: missing boxes + label POSSIBLE DROWNING!
    if draw_red and missing_boxes:
        for mb in missing_boxes:
            x1, y1, x2, y2 = _px(mb, W, H)
            draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=7)
            draw.text((x1 + 6, max(62, y1 - 18)), "POSSIBLE DROWNING!", fill=(255, 0, 0))

    out_buf = io.BytesIO()
    img.save(out_buf, format="PNG")
    out_buf.seek(0)
    return out_buf.getvalue()


def put_png_and_presign(bucket, key, png_bytes):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=png_bytes,
        ContentType="image/png"
    )
    return presign_get_url(bucket, key)


# =========================
# List frames numeric order (1.png,2.png,)
# =========================
def list_frames_numeric(bucket, prefix, max_frames):
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    frames = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or (not _is_image_key(key)):
                continue

            filename = key.split("/")[-1].lower()
            m = re.match(r"^(\d+)\.(png|jpg|jpeg)$", filename)
            if not m:
                continue

            frames.append((int(m.group(1)), key))

    frames.sort(key=lambda x: x[0])
    keys = [k for _, k in frames]
    if max_frames and len(keys) > max_frames:
        keys = keys[:max_frames]
    return keys


# =========================
# Lambda Handler
# =========================
def lambda_handler(event, context):
    prefix            = FRAMES_PREFIX
    testingset_prefix = TESTINGSET_PREFIX
    warning_prefix    = WARNING_PREFIX
    max_frames        = MAX_FRAMES

    if isinstance(event, dict):
        prefix            = event.get("prefix", prefix)
        testingset_prefix = event.get("testingset_prefix", testingset_prefix)
        warning_prefix    = event.get("warning_prefix", warning_prefix)
        max_frames        = int(event.get("max_frames", max_frames))

    frame_keys = list_frames_numeric(BUCKET, prefix, max_frames)
    if not frame_keys:
        return _resp(200, {"status": "NO_FRAMES", "bucket": BUCKET, "prefix": prefix})

    outputs = []
    alerts  = []

    prev_key   = None
    prev_boxes = None
    prev_count = None

    # keep previous TestingSet key (so we can save prevImageKey in the event)
    prev_testing_key = None

    # ===== ACTIVE ALERT STATE (based ONLY on COUNTER baseline) =====
    baseline_count       = None
    active_missing_boxes = []
    active_from_prev_key = None

    for key in frame_keys:
        curr_boxes = detect_person_boxes(BUCKET, key)
        curr_count = len(curr_boxes)

        drop_by = 0

        # Start/Update missing state ONLY when counter drops
        if prev_count is not None and curr_count < prev_count:
            drop_by = prev_count - curr_count

            if baseline_count is None:
                baseline_count = prev_count

            missing_candidates = find_missing_boxes(prev_boxes, curr_boxes)

            # FORCE: if counter dropped but no missing found -> choose from prev_boxes anyway
            if drop_by > 0 and (not missing_candidates):
                missing_candidates = prev_boxes[:] if prev_boxes else []

            active_missing_boxes = pick_top_missing(prev_boxes, curr_boxes, missing_candidates, drop_by)
            active_from_prev_key = prev_key

            alerts.append({
                "type": "COUNTER_DROP",
                "frame_now": key,
                "prev_frame": prev_key,
                "baseline_count": baseline_count,
                "prev_count": prev_count,
                "curr_count": curr_count,
                "drop_by": drop_by,
                "missing_boxes_last_seen": active_missing_boxes
            })

        # Clear once recovered
        if baseline_count is not None and curr_count >= baseline_count:
            baseline_count = None
            active_missing_boxes = []
            active_from_prev_key = None

        is_alert = (baseline_count is not None and curr_count < baseline_count and len(active_missing_boxes) > 0)

        status_label = "ALERT" if is_alert else "OK"
        title = f"{status_label} | Frame: {key} | count={curr_count}" + (f" | baseline={baseline_count}" if baseline_count is not None else "")

        # ===== Build TestingSet image (GREEN + RED) =====
        testing_png = render_png(
            src_bucket=BUCKET,
            src_key=key,
            title=title,
            curr_boxes=curr_boxes,
            missing_boxes=(active_missing_boxes if is_alert else []),
            draw_green=True,
            draw_red=True
        )

        testing_key = f"{testingset_prefix}{_basename(key)}_{status_label}.png"
        testing_url = put_png_and_presign(BUCKET, testing_key, testing_png)

        # OPTIONAL: still create red-only warning image for archive/debug
        archive_warning_key = None
        archive_warning_url = None

        created_event_id = None

        if is_alert:
            # ===== Create optional Warning/ red-only image (archive) =====
            warning_title = f"ALERT | Frame: {key} | POSSIBLE DROWNING!"
            warning_png = render_png(
                src_bucket=BUCKET,
                src_key=key,
                title=warning_title,
                curr_boxes=[],
                missing_boxes=active_missing_boxes,
                draw_green=False,
                draw_red=True
            )

            archive_warning_key = f"{warning_prefix}{_basename(key)}_WARNING.png"
            archive_warning_url = put_png_and_presign(BUCKET, archive_warning_key, warning_png)

            # ===== Create Event in DB (IMPORTANT: warningImageKey points to TESTINGSET) =====
            created_event_id = f"EVT-{int(time.time())}-{_basename(key)}"
            created_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            item = {
                "eventId": created_event_id,
                "status": "OPEN",
                "created_at": created_at_iso,

                # ✅ This is what the client should show (TestingSet ALERT image)
                "warningImageKey": testing_key,
            }
            if prev_testing_key:
                item["prevImageKey"] = prev_testing_key

            wrote_db = False
            try:
                events_table.put_item(Item=item)
                wrote_db = True
                print(f"[DB] Event created: {created_event_id} -> prev={prev_testing_key} warning(TESTINGSET)={testing_key}")
            except Exception as e:
                print(f"[DB ERROR] Failed to write event to DynamoDB: {str(e)}")

            # ===== SNS Email: send BEFORE/AFTER from TESTINGSET (not Warning/) =====
            if wrote_db:
                prev_url_for_sns = presign_get_url(BUCKET, prev_testing_key) if prev_testing_key else None

                publish_sns_alert(
                    event_id=created_event_id,
                    created_at_iso=created_at_iso,

                    # keys/urls of TestingSet BEFORE/AFTER:
                    prev_key=prev_testing_key,
                    warn_key=testing_key,
                    prev_url=prev_url_for_sns,
                    warn_url=testing_url
                )

        outputs.append({
            "frame": key,
            "count": curr_count,
            "baseline_count": baseline_count,
            "is_alert": is_alert,
            "drop_by": drop_by,

            "testing_key": testing_key,
            "testing_url": testing_url,

            # archive only (optional)
            "warning_key": archive_warning_key,
            "warning_url": archive_warning_url,

            "eventId": created_event_id,
            "missing_boxes_last_seen": (active_missing_boxes if is_alert else []),
            "missing_from_prev_frame": active_from_prev_key,

            "prev_testing_key": prev_testing_key,
        })

        # advance
        prev_key = key
        prev_boxes = curr_boxes
        prev_count = curr_count

        # store current frame testing key as "previous" for next loop
        prev_testing_key = testing_key

    return _resp(200, {
        "status": "TESTINGSET_WARNING_AND_EVENTS_CREATED",
        "bucket": BUCKET,
        "frames_prefix": prefix,
        "testingset_prefix": testingset_prefix,
        "warning_prefix": warning_prefix,
        "events_table": EVENTS_TABLE_NAME,
        "sns_topic_arn_set": bool(SNS_TOPIC_ARN),
        "total_frames": len(frame_keys),
        "outputs_count": len(outputs),
        "outputs": outputs,
        "alerts_count": len(alerts),
        "alerts": alerts
    })
