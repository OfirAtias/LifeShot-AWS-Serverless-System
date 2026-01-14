import boto3
import json
import os
import re
import math
import time
from datetime import datetime, timezone
from botocore.exceptions import ClientError

# =========================
# ENV CONFIG
# =========================
BUCKET = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")

FRAMES_PREFIX = os.getenv("FRAMES_PREFIX", "LifeShot/")  # input frames
DROWNINGSET_PREFIX = os.getenv("DROWNINGSET_PREFIX", "LifeShot/DrowningSet/")

MAX_FRAMES = int(os.getenv("MAX_FRAMES", "200"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "70"))

MIN_BOX_AREA = float(os.getenv("MIN_BOX_AREA", "0.0015"))
MAX_BOX_AREA = float(os.getenv("MAX_BOX_AREA", "0.70"))

MATCH_IOU_MIN = float(os.getenv("MATCH_IOU_MIN", "0.08"))
MATCH_CENTER_MAX = float(os.getenv("MATCH_CENTER_MAX", "0.12"))

PRESIGN_EXPIRES = int(os.getenv("PRESIGN_EXPIRES", "3600"))

# (optional) used only for prevImageUrl presign
# Events lambda already has its own EVENTS_TABLE_NAME + SNS_TOPIC_ARN in env vars
# so detector does NOT need SNS env at all
EVENTS_TABLE_NAME = os.getenv("EVENTS_TABLE_NAME", "LifeShot_Events")

# ✅ Names of the other two Lambdas (matches your env vars screenshots)
RENDER_LAMBDA_NAME = os.getenv("RENDER_LAMBDA_NAME", "LifeShot_RenderAndS3")
EVENTS_LAMBDA_NAME = os.getenv("EVENTS_LAMBDA_NAME", "LifeShot_EventsAndSNS")

rekognition = boto3.client("rekognition")
s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")


# =========================
# Helpers
# =========================
def _resp(code, body_obj):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body_obj),
    }


def _is_image_key(key: str) -> bool:
    k = key.lower()
    return k.endswith(".png") or k.endswith(".jpg") or k.endswith(".jpeg")


def _basename(key: str) -> str:
    name = key.split("/")[-1]
    name = re.sub(r"\.(png|jpg|jpeg)$", "", name, flags=re.IGNORECASE)
    return name


def _center(b):
    return (
        float(b.get("Left", 0)) + float(b.get("Width", 0)) / 2.0,
        float(b.get("Top", 0)) + float(b.get("Height", 0)) / 2.0,
    )


def _dist(a, b):
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)


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

    a_area = (ax2 - ax1) * (ay2 - ay1)
    b_area = (bx2 - bx1) * (by2 - by1)
    union = max(1e-9, a_area + b_area - inter)
    return inter / union


def presign_get_url(bucket, key):
    if not key:
        return None
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_EXPIRES,
        )
    except ClientError:
        return None


# =========================
# Rekognition detection
# =========================
def detect_person_boxes(bucket, key):
    try:
        res = rekognition.detect_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MaxLabels=25,
            MinConfidence=MIN_CONFIDENCE,
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

                boxes.append(
                    {
                        "Left": float(box.get("Left", 0)),
                        "Top": float(box.get("Top", 0)),
                        "Width": w,
                        "Height": h,
                        "Conf": float(inst.get("Confidence", 0)),
                    }
                )

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

        score = (1.0 - best_iou) + best_dist
        scored.append((score, pb))

    scored.sort(reverse=True, key=lambda x: x[0])
    return [pb for _, pb in scored[:drop_by]]


# ===============================
# List frames — BY LastModified
# ===============================
def list_frames_numeric(bucket, prefix, max_frames):
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    frames = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or (not _is_image_key(key)):
                continue
            lm = obj.get("LastModified")
            if lm is None:
                lm = datetime.min.replace(tzinfo=timezone.utc)
            frames.append((lm, key))

    frames.sort(key=lambda x: x[0])  # oldest -> newest
    keys = [k for _, k in frames]
    if max_frames and len(keys) > max_frames:
        keys = keys[:max_frames]
    return keys


# =========================
# Normalize event (Function URL)
# =========================
def _normalize_event(event):
    if not isinstance(event, dict):
        return {}
    if "body" not in event:
        return event

    body = event.get("body") or "{}"

    if event.get("isBase64Encoded"):
        try:
            import base64
            body = base64.b64decode(body).decode("utf-8")
        except Exception:
            body = "{}"

    if isinstance(body, dict):
        return body

    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


# =========================
# Invoke helpers
# =========================
def invoke_render_lambda(payload: dict) -> dict:
    resp = lambda_client.invoke(
        FunctionName=RENDER_LAMBDA_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    raw = resp["Payload"].read().decode("utf-8") or "{}"
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "error": "render_lambda_invalid_json", "raw": raw}


def invoke_events_lambda(payload: dict) -> dict:
    resp = lambda_client.invoke(
        FunctionName=EVENTS_LAMBDA_NAME,
        InvocationType="Event",  # async
        Payload=json.dumps(payload).encode("utf-8"),
    )
    return {"invoked": True, "status_code": resp.get("StatusCode")}


# =========================
# Lambda Handler
# =========================
def lambda_handler(event, context):
    event = _normalize_event(event)

    prefix = FRAMES_PREFIX
    drowningset_prefix = DROWNINGSET_PREFIX
    max_frames = MAX_FRAMES

    if isinstance(event, dict):
        scene = event.get("scene")
        if scene == 1:
            prefix = "LifeShot/Test1/"
            drowningset_prefix = "LifeShot/DrowningSet/Test1/"
        elif scene == 2:
            prefix = "LifeShot/Test2/"
            drowningset_prefix = "LifeShot/DrowningSet/Test2/"

        prefix = event.get("prefix", prefix)

        if "drowningset_prefix" in event:
            drowningset_prefix = event.get("drowningset_prefix", drowningset_prefix)
        elif "testingset_prefix" in event:
            drowningset_prefix = event.get("testingset_prefix", drowningset_prefix)

        max_frames = int(event.get("max_frames", max_frames))

    frame_keys = list_frames_numeric(BUCKET, prefix, max_frames)
    if not frame_keys:
        return _resp(200, {"status": "NO_FRAMES", "bucket": BUCKET, "prefix": prefix})

    outputs = []
    alerts = []

    prev_key = None
    prev_boxes = None
    prev_count = None
    prev_drowningset_key = None

    baseline_count = None
    active_missing_boxes = []
    active_from_prev_key = None

    for key in frame_keys:
        curr_boxes = detect_person_boxes(BUCKET, key)
        curr_count = len(curr_boxes)
        drop_by = 0

        if prev_count is not None and curr_count < prev_count:
            drop_by = prev_count - curr_count
            if baseline_count is None:
                baseline_count = prev_count

            missing_candidates = find_missing_boxes(prev_boxes, curr_boxes)

            if drop_by > 0 and (not missing_candidates):
                missing_candidates = prev_boxes[:] if prev_boxes else []

            active_missing_boxes = pick_top_missing(
                prev_boxes, curr_boxes, missing_candidates, drop_by
            )
            active_from_prev_key = prev_key

            alerts.append(
                {
                    "type": "COUNTER_DROP",
                    "frame_now": key,
                    "prev_frame": prev_key,
                    "baseline_count": baseline_count,
                    "prev_count": prev_count,
                    "curr_count": curr_count,
                    "drop_by": drop_by,
                    "missing_boxes_last_seen": active_missing_boxes,
                }
            )

        if baseline_count is not None and curr_count >= baseline_count:
            baseline_count = None
            active_missing_boxes = []
            active_from_prev_key = None

        is_alert = (
            baseline_count is not None
            and curr_count < baseline_count
            and len(active_missing_boxes) > 0
        )

        status_label = "ALERT" if is_alert else "OK"
        title = (
            f"{status_label} | Frame: {key} | count={curr_count}"
            + (f" | baseline={baseline_count}" if baseline_count is not None else "")
        )

        drowningset_key = f"{drowningset_prefix}{_basename(key)}_{status_label}.png"

        # ✅ call Render lambda (draw + S3 + presign)
        render_payload = {
            "bucket": BUCKET,
            "src_key": key,
            "out_key": drowningset_key,
            "title": title,
            "curr_boxes": curr_boxes,
            "missing_boxes": (active_missing_boxes if is_alert else []),
            "presign_expires": PRESIGN_EXPIRES,
        }
        render_res = invoke_render_lambda(render_payload)
        drowningset_url = render_res.get("out_url")
        render_ok = bool(render_res.get("ok"))

        created_event_id = None

        if is_alert and render_ok:
            created_event_id = f"EVT-{int(time.time())}-{_basename(key)}"
            created_at_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            prev_url_for_sns = (
                presign_get_url(BUCKET, prev_drowningset_key) if prev_drowningset_key else None
            )

            # ✅ invoke Events lambda (DDB + SNS)
            # NOTE: sns_topic_arn REMOVED - EventsAndSNS takes it from its own env vars
            events_payload = {
                "eventId": created_event_id,
                "created_at": created_at_iso,
                "status": "OPEN",
                "events_table": EVENTS_TABLE_NAME,  # optional
                "bucket": BUCKET,
                "warningImageKey": drowningset_key,
                "warningImageUrl": drowningset_url,
                "prevImageKey": prev_drowningset_key,
                "prevImageUrl": prev_url_for_sns,
            }
            invoke_events_lambda(events_payload)

        outputs.append(
            {
                "frame": key,
                "count": curr_count,
                "baseline_count": baseline_count,
                "is_alert": is_alert,
                "drop_by": drop_by,
                "drowningset_key": drowningset_key,
                "drowningset_url": drowningset_url,
                "render_ok": render_ok,
                "eventId": created_event_id,
                "missing_boxes_last_seen": (active_missing_boxes if is_alert else []),
                "missing_from_prev_frame": active_from_prev_key,
                "prev_drowningset_key": prev_drowningset_key,
            }
        )

        prev_key = key
        prev_boxes = curr_boxes
        prev_count = curr_count
        prev_drowningset_key = drowningset_key

    return _resp(
        200,
        {
            "status": "DROWNINGSET_AND_EVENTS_CREATED",
            "bucket": BUCKET,
            "frames_prefix": prefix,
            "drowningset_prefix": drowningset_prefix,
            "render_lambda": RENDER_LAMBDA_NAME,
            "events_lambda": EVENTS_LAMBDA_NAME,
            "total_frames": len(frame_keys),
            "outputs_count": len(outputs),
            "outputs": outputs,
            "alerts_count": len(alerts),
            "alerts": alerts,
        },
    )
