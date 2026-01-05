import boto3
import json
import os
import re
import math
import io
from PIL import Image, ImageDraw

# =========================
# ENV CONFIG
# =========================
BUCKET          = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")
FRAMES_PREFIX   = os.getenv("FRAMES_PREFIX", "LifeShot/")                 # input frames
OUT_PREFIX      = os.getenv("OUT_PREFIX", "LifeShot/Annotated/")          # annotated output (all frames)
WARNINGS_PREFIX = os.getenv("WARNINGS_PREFIX", "LifeShot/Warnings/")      # duplicate output (all frames, OK/ALERT)

MAX_FRAMES      = int(os.getenv("MAX_FRAMES", "200"))
MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "70"))

# Filter tiny/huge boxes (normalized area)
MIN_BOX_AREA    = float(os.getenv("MIN_BOX_AREA", "0.0015"))
MAX_BOX_AREA    = float(os.getenv("MAX_BOX_AREA", "0.70"))

# Matching params (used only to pick "where disappeared")
MATCH_IOU_MIN    = float(os.getenv("MATCH_IOU_MIN", "0.08"))
MATCH_CENTER_MAX = float(os.getenv("MATCH_CENTER_MAX", "0.12"))

PRESIGN_EXPIRES = int(os.getenv("PRESIGN_EXPIRES", "3600"))

rekognition = boto3.client("rekognition")
s3          = boto3.client("s3")


# =========================
# Lambda Handler
# =========================
def lambda_handler(event, context):
    prefix          = FRAMES_PREFIX
    out_prefix      = OUT_PREFIX
    warnings_prefix = WARNINGS_PREFIX
    max_frames      = MAX_FRAMES

    if isinstance(event, dict):
        prefix          = event.get("prefix", prefix)
        out_prefix      = event.get("out_prefix", out_prefix)
        warnings_prefix = event.get("warnings_prefix", warnings_prefix)
        max_frames      = int(event.get("max_frames", max_frames))

    frame_keys = list_frames_numeric(BUCKET, prefix, max_frames)
    if not frame_keys:
        return _resp(200, {"status": "NO_FRAMES", "bucket": BUCKET, "prefix": prefix})

    outputs = []
    alerts  = []

    prev_key   = None
    prev_boxes = None
    prev_count = None

    # ===== ACTIVE "DROWNING" STATE (based ONLY on counter) =====
    baseline_count       = None          # count BEFORE drop
    active_missing_boxes = []            # last-seen boxes (from baseline-1 frame)
    active_from_prev_key = None          # where last-seen was taken from (prev frame when drop happened)

    for key in frame_keys:
        curr_boxes = detect_person_boxes(BUCKET, key)
        curr_count = len(curr_boxes)

        drop_by = 0
        new_missing_boxes = []

        # ===== Start/Update missing state ONLY when counter drops =====
        if prev_count is not None and curr_count < prev_count:
            drop_by = prev_count - curr_count

            # set baseline when first drop happens (or if no active)
            if baseline_count is None:
                baseline_count = prev_count

            # locate last-seen boxes from prev frame
            new_missing_boxes = find_missing_boxes(prev_boxes, curr_boxes)

            # FORCE: if counter dropped but none found -> choose candidates from prev_boxes anyway
            if drop_by > 0 and (not new_missing_boxes):
                new_missing_boxes = prev_boxes[:] if prev_boxes else []

            new_missing_boxes = pick_top_missing(prev_boxes, curr_boxes, new_missing_boxes, drop_by)

            # activate / extend missing boxes
            active_missing_boxes = new_missing_boxes[:]  # for your use-case drop_by=1 typically
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

        # ===== Keep marking red in subsequent frames until counter returns to baseline =====
        # If baseline exists and we are still below it, keep alert active.
        # If count recovered (>= baseline), clear alert.
        if baseline_count is not None:
            if curr_count >= baseline_count:
                baseline_count = None
                active_missing_boxes = []
                active_from_prev_key = None

        is_alert = (baseline_count is not None and curr_count < baseline_count and len(active_missing_boxes) > 0)

        title = build_title(
            frame_key=key,
            curr_count=curr_count,
            baseline_count=baseline_count,
            is_alert=is_alert,
            drop_by=drop_by
        )

        # ===== Create ONE annotated image bytes, then save twice (Annotated + Warnings) =====
        annotated_png = render_annotated_png(
            src_bucket=BUCKET,
            src_key=key,
            curr_boxes=curr_boxes,
            missing_boxes=(active_missing_boxes if is_alert else []),
            title=title,
            missing_from_frame=active_from_prev_key
        )

        out_key = f"{out_prefix}{_basename(key)}_ANNOT.png"
        warnings_key = f"{warnings_prefix}{_basename(key)}_{'ALERT' if is_alert else 'OK'}.png"

        out_url = put_png_and_presign(BUCKET, out_key, annotated_png)
        warn_url = put_png_and_presign(BUCKET, warnings_key, annotated_png)

        outputs.append({
            "frame": key,
            "count": curr_count,
            "baseline_count": baseline_count,
            "is_alert": is_alert,
            "drop_by": drop_by,

            "annotated_out_key": out_key,
            "annotated_out_url": out_url,

            "warnings_key": warnings_key,
            "warnings_url": warn_url,

            "missing_boxes_last_seen": (active_missing_boxes if is_alert else [])
        })

        # advance
        prev_key = key
        prev_boxes = curr_boxes
        prev_count = curr_count

    return _resp(200, {
        "status": "ANNOTATED_COUNTER_DROP_WITH_WARNINGS",
        "bucket": BUCKET,
        "frames_prefix": prefix,
        "out_prefix": out_prefix,
        "warnings_prefix": warnings_prefix,
        "total_frames": len(frame_keys),
        "outputs_count": len(outputs),
        "outputs": outputs,
        "alerts_count": len(alerts),
        "alerts": alerts
    })


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
                    "Height": h
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
    """
    Counter dropped by K => we must return K boxes to mark red.
    If we have many candidates, pick the WORST-matching ones.
    If we have too few, return what we have (caller ensures candidates exist).
    """
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
def render_annotated_png(src_bucket, src_key, curr_boxes, missing_boxes, title, missing_from_frame):
    img_bytes = s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # Title bar
    draw.rectangle([0, 0, W, 58], fill=(0, 0, 0))
    draw.text((12, 18), title, fill=(255, 255, 255))

    # GREEN: current detections
    for b in curr_boxes:
        x1, y1, x2, y2 = _px(b, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=4)

    # RED: missing (last seen box)
    if missing_boxes:
        for mb in missing_boxes:
            x1, y1, x2, y2 = _px(mb, W, H)
            draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=7)
            label = "DROWNING ALERT (COUNTER DROP)"
            if missing_from_frame:
                label += f" | last_seen={missing_from_frame}"
            draw.text((x1 + 6, max(62, y1 - 18)), label, fill=(255, 0, 0))

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
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=PRESIGN_EXPIRES
    )


def build_title(frame_key, curr_count, baseline_count, is_alert, drop_by):
    # Example:
    # Frame: LifeShot/5.png | count=7 | baseline=8 | ALERT_ACTIVE | DROP_BY=0
    t = f"Frame: {frame_key} | count={curr_count}"
    if baseline_count is not None:
        t += f" | baseline={baseline_count}"
    if is_alert:
        t += " | ALERT_ACTIVE"
    if drop_by > 0:
        t += f" | DROP_BY={drop_by}"
    return t


# =========================
# List frames numeric order (1.png,2.png,...)
# =========================
def list_frames_numeric(bucket, prefix, max_frames):
    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    frames = []
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/") or not _is_image_key(key):
                continue

            filename = key.split("/")[-1].lower()
            m = re.match(r"^(\d+)\.(png|jpg|jpeg|webp)$", filename)
            if not m:
                continue

            frames.append((int(m.group(1)), key))

    frames.sort(key=lambda x: x[0])
    keys = [k for _, k in frames]
    if max_frames > 0:
        keys = keys[:max_frames]
    return keys


def _is_image_key(key: str) -> bool:
    k = key.lower()
    return k.endswith(".png") or k.endswith(".jpg") or k.endswith(".jpeg") or k.endswith(".webp")


def _basename(key: str) -> str:
    name = key.split("/")[-1]
    return name.rsplit(".", 1)[0]


# =========================
# Geometry helpers
# =========================
def _px(box, W, H):
    x1 = int(box["Left"] * W)
    y1 = int(box["Top"] * H)
    x2 = int((box["Left"] + box["Width"]) * W)
    y2 = int((box["Top"] + box["Height"]) * H)
    return x1, y1, x2, y2


def _center(b):
    return (b["Left"] + b["Width"] / 2.0, b["Top"] + b["Height"] / 2.0)


def _dist(p1, p2):
    return math.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)


def _iou(a, b):
    ax1, ay1 = a["Left"], a["Top"]
    ax2, ay2 = a["Left"] + a["Width"], a["Top"] + a["Height"]

    bx1, by1 = b["Left"], b["Top"]
    bx2, by2 = b["Left"] + b["Width"], b["Top"] + b["Height"]

    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)

    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h

    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)

    denom = area_a + area_b - inter_area
    if denom <= 0:
        return 0.0
    return inter_area / denom


# =========================
# Response helper
# =========================
def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False)
    }
