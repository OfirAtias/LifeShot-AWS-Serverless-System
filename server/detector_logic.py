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
BUCKET        = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")
FRAMES_PREFIX = os.getenv("FRAMES_PREFIX", "LifeShot/")            # input frames location
OUT_PREFIX    = os.getenv("OUT_PREFIX", "LifeShot/Annotated/")     # output images folder

MAX_FRAMES     = int(os.getenv("MAX_FRAMES", "200"))
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "70"))

# Filter tiny/huge boxes (normalized area)
MIN_BOX_AREA   = float(os.getenv("MIN_BOX_AREA", "0.0015"))
MAX_BOX_AREA   = float(os.getenv("MAX_BOX_AREA", "0.70"))

# Matching params (only used to find "where he disappeared" AFTER counter drop)
MATCH_IOU_MIN    = float(os.getenv("MATCH_IOU_MIN", "0.08"))
MATCH_CENTER_MAX = float(os.getenv("MATCH_CENTER_MAX", "0.12"))

PRESIGN_EXPIRES = int(os.getenv("PRESIGN_EXPIRES", "3600"))

rekognition = boto3.client("rekognition")
s3          = boto3.client("s3")


# =========================
# Lambda Handler
# =========================
def lambda_handler(event, context):
    prefix     = FRAMES_PREFIX
    out_prefix = OUT_PREFIX
    max_frames = MAX_FRAMES

    if isinstance(event, dict):
        prefix     = event.get("prefix", prefix)
        out_prefix = event.get("out_prefix", out_prefix)
        max_frames = int(event.get("max_frames", max_frames))

    frame_keys = list_frames_numeric(BUCKET, prefix, max_frames)
    if not frame_keys:
        return _resp(200, {"status": "NO_FRAMES", "bucket": BUCKET, "prefix": prefix})

    outputs = []
    alerts  = []

    prev_key   = None
    prev_boxes = None
    prev_count = None

    for key in frame_keys:
        curr_boxes = detect_person_boxes(BUCKET, key)
        curr_count = len(curr_boxes)

        missing_boxes = []
        drop_by = 0

        # ONLY use COUNTER to decide disappearance
        if prev_count is not None and curr_count < prev_count:
            drop_by = prev_count - curr_count

            # Now locate where the disappeared person(s) were last seen:
            # find boxes from prev that don't match anything in current
            missing_boxes = find_missing_boxes(prev_boxes, curr_boxes)

            # If we have more unmatched than drop_by, keep the most "likely missing":
            # choose the ones with worst match score (no/low IoU and far distance)
            missing_boxes = pick_top_missing(prev_boxes, curr_boxes, missing_boxes, drop_by)

            alerts.append({
                "frame_now": key,
                "prev_frame": prev_key,
                "prev_count": prev_count,
                "curr_count": curr_count,
                "drop_by": drop_by,
                "missing_boxes_last_seen": missing_boxes
            })

        title = (
            f"Frame: {key} | count={curr_count}"
            + (f" | DROP_BY={drop_by}" if drop_by > 0 else "")
        )

        out_key = f"{out_prefix}{_basename(key)}_ANNOT.png"
        out_url = create_annotated_image(
            src_bucket=BUCKET,
            src_key=key,
            out_bucket=BUCKET,
            out_key=out_key,
            curr_boxes=curr_boxes,       # GREEN
            missing_boxes=missing_boxes, # RED (from prev, only if counter dropped)
            title=title
        )

        outputs.append({
            "frame": key,
            "out_key": out_key,
            "out_url": out_url,
            "count": curr_count,
            "drop_by": drop_by,
            "missing_count": len(missing_boxes)
        })

        # advance
        prev_key = key
        prev_boxes = curr_boxes
        prev_count = curr_count

    return _resp(200, {
        "status": "ANNOTATED_WITH_COUNTER_DROP",
        "bucket": BUCKET,
        "frames_prefix": prefix,
        "out_prefix": out_prefix,
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
        # everything missing
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
    If counter dropped by K, we only want K missing boxes (last seen).
    When there are multiple unmatched boxes (noise), pick the ones with the WORST match score.
    """
    if drop_by <= 0:
        return []
    if len(missing_candidates) <= drop_by:
        return missing_candidates

    scored = []
    for pb in missing_candidates:
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

        # worse = low iou and high dist
        score = (1.0 - best_iou) + best_dist
        scored.append((score, pb))

    scored.sort(reverse=True, key=lambda x: x[0])  # worst first
    return [pb for _, pb in scored[:drop_by]]


# =========================
# Annotate and save image
# =========================
def create_annotated_image(src_bucket, src_key, out_bucket, out_key, curr_boxes, missing_boxes, title):
    img_bytes = s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # Title bar
    draw.rectangle([0, 0, W, 52], fill=(0, 0, 0))
    draw.text((12, 16), title, fill=(255, 255, 255))

    # GREEN: current detections
    for b in curr_boxes:
        x1, y1, x2, y2 = _px(b, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=4)

    # RED: missing (last seen from previous frame), ONLY when counter dropped
    for mb in missing_boxes:
        x1, y1, x2, y2 = _px(mb, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=7)
        draw.text((x1 + 6, max(56, y1 - 16)), "MISSING (COUNTER DROP)", fill=(255, 0, 0))

    out_buf = io.BytesIO()
    img.save(out_buf, format="PNG")
    out_buf.seek(0)

    s3.put_object(
        Bucket=out_bucket,
        Key=out_key,
        Body=out_buf.getvalue(),
        ContentType="image/png"
    )

    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": out_bucket, "Key": out_key},
        ExpiresIn=PRESIGN_EXPIRES
    )


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
