import boto3
import json
import os
import re
import time
import math
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
import io

# =========================
# CONFIG (Env Vars)
# =========================
FRAMES_BUCKET   = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")
FRAMES_PREFIX   = os.getenv("FRAMES_PREFIX", "LifeShot/")   # where 1.png.. are
OUT_PREFIX      = os.getenv("OUT_PREFIX", "LifeShot/Annotated/")

CAMERA_ID       = os.getenv("CAMERA_ID", "cam-01")

MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "70"))
MAX_FRAMES      = int(os.getenv("MAX_FRAMES", "50"))

# Heuristics filter on boxes (0..1 relative to image)
MIN_BOX_AREA    = float(os.getenv("MIN_BOX_AREA", "0.002"))
MAX_BOX_AREA    = float(os.getenv("MAX_BOX_AREA", "0.70"))

# Matching between frames
# If a prev box has no match in next frame -> "missing"
MATCH_IOU_MIN   = float(os.getenv("MATCH_IOU_MIN", "0.08"))
MATCH_CENTER_MAX= float(os.getenv("MATCH_CENTER_MAX", "0.12"))  # distance in normalized coords

# If you want "missing" only when count drops by at least 1
REQUIRE_DROP    = os.getenv("REQUIRE_DROP", "true").lower() == "true"

# Presigned URL expiry seconds
PRESIGN_EXPIRES = int(os.getenv("PRESIGN_EXPIRES", "3600"))  # 1 hour

rekognition = boto3.client("rekognition")
s3          = boto3.client("s3")

# =========================
# Lambda Handler
# =========================
def lambda_handler(event, context):
    prefix = FRAMES_PREFIX
    max_frames = MAX_FRAMES

    if isinstance(event, dict):
        prefix = event.get("prefix", prefix)
        max_frames = int(event.get("max_frames", max_frames))

    # 1) list frames in numeric order (1.png, 2.png, ...)
    frame_keys = list_frames_numeric(FRAMES_BUCKET, prefix, max_frames)
    if not frame_keys:
        return _resp(200, {"status": "NO_FRAMES", "bucket": FRAMES_BUCKET, "prefix": prefix})

    # 2) process frames: detect persons -> annotate -> upload
    results = []
    missing_events = []

    prev_boxes = None
    prev_key = None

    for key in frame_keys:
        # detect persons in this frame
        boxes = detect_person_boxes(FRAMES_BUCKET, key)

        # always create an annotated image for this frame (all detected people)
        annotated_key = f"{OUT_PREFIX}{_basename(key)}_DETECTIONS.png"
        annotated_url = annotate_and_upload(
            src_bucket=FRAMES_BUCKET,
            src_key=key,
            out_bucket=FRAMES_BUCKET,
            out_key=annotated_key,
            boxes=boxes,
            title=f"DETECTIONS: {key} | count={len(boxes)}",
            highlight=None
        )

        frame_info = {
            "frame": key,
            "detected_count": len(boxes),
            "detections_image_key": annotated_key,
            "detections_image_url": annotated_url
        }

        # compare with previous frame to find missing
        if prev_boxes is not None:
            # optional: only if count dropped
            if (not REQUIRE_DROP) or (len(boxes) < len(prev_boxes)):
                missing_prev_idxs = find_missing(prev_boxes, boxes)

                # for each missing person, create an extra image on PREVIOUS frame with red highlight
                for miss_idx in missing_prev_idxs:
                    miss_box = prev_boxes[miss_idx]
                    missing_img_key = f"{OUT_PREFIX}{_basename(prev_key)}_MISSING.png"

                    missing_url = annotate_and_upload(
                        src_bucket=FRAMES_BUCKET,
                        src_key=prev_key,                 # last seen frame
                        out_bucket=FRAMES_BUCKET,
                        out_key=missing_img_key,
                        boxes=prev_boxes,                 # draw all previous detections
                        title=f"MISSING DETECTED: next={key} | last_seen={prev_key}",
                        highlight={
                            "box": miss_box,
                            "label": "LAST SEEN (MISSING NEXT FRAME)"
                        }
                    )

                    missing_events.append({
                        "missing_detected_on_frame": key,
                        "last_seen_frame": prev_key,
                        "last_seen_box": miss_box,
                        "missing_image_key": missing_img_key,
                        "missing_image_url": missing_url
                    })

        results.append(frame_info)

        prev_boxes = boxes
        prev_key = key

    return _resp(200, {
        "status": "ANNOTATED",
        "bucket": FRAMES_BUCKET,
        "frames_prefix": prefix,
        "output_prefix": OUT_PREFIX,
        "total_frames": len(frame_keys),
        "frames": results,
        "missing_events_count": len(missing_events),
        "missing_events": missing_events
    })

# =========================
# S3 listing in numeric order
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

            filename = key.split("/")[-1]
            m = re.match(r"^(\d+)\.(png|jpg|jpeg|webp)$", filename.lower())
            if not m:
                continue

            frame_num = int(m.group(1))
            frames.append((frame_num, key))

    frames.sort(key=lambda x: x[0])
    keys = [k for _, k in frames]
    if max_frames > 0:
        keys = keys[:max_frames]
    return keys

def _is_image_key(key: str) -> bool:
    k = key.lower()
    return k.endswith(".png") or k.endswith(".jpg") or k.endswith(".jpeg") or k.endswith(".webp")

def _basename(key: str) -> str:
    # LifeShot/12.png -> 12
    name = key.split("/")[-1]
    return name.rsplit(".", 1)[0]

# =========================
# Rekognition detection
# =========================
def detect_person_boxes(bucket, key):
    """
    Returns list of BoundingBox dicts: {"Left","Top","Width","Height"} in normalized [0..1]
    """
    try:
        res = rekognition.detect_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MaxLabels=25,
            MinConfidence=MIN_CONFIDENCE
        )

        boxes = []
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
# Missing logic: match prev boxes to curr boxes
# =========================
def find_missing(prev_boxes, curr_boxes):
    """
    Returns indices of prev_boxes that have no good match in curr_boxes.
    Match by IoU OR center distance.
    Greedy matching: for each prev, pick best curr.
    """
    missing = []

    for i, pb in enumerate(prev_boxes):
        best_iou = 0.0
        best_center = 999.0

        pc = _center(pb)
        for cb in curr_boxes:
            iou = _iou(pb, cb)
            cc = _center(cb)
            dist = _dist(pc, cc)

            if iou > best_iou:
                best_iou = iou
            if dist < best_center:
                best_center = dist

        matched = (best_iou >= MATCH_IOU_MIN) or (best_center <= MATCH_CENTER_MAX)
        if not matched:
            missing.append(i)

    return missing

def _center(b):
    return (b["Left"] + b["Width"]/2.0, b["Top"] + b["Height"]/2.0)

def _dist(p1, p2):
    return math.sqrt((p1[0]-p2[0])**2 + (p1[1]-p2[1])**2)

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
# Annotation + Upload
# =========================
def annotate_and_upload(src_bucket, src_key, out_bucket, out_key, boxes, title=None, highlight=None):
    """
    Downloads image, draws boxes, uploads annotated image,
    returns a presigned URL.
    """
    img_bytes = s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)

    W, H = img.size

    # Draw all detections in green with an index number
    for idx, box in enumerate(boxes, start=1):
        x1, y1, x2, y2 = _px(box, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=4)
        draw.text((x1 + 4, max(0, y1 - 18)), f"#{idx}", fill=(0, 255, 0))

    # Highlight missing (last seen) in red
    if highlight and "box" in highlight:
        mb = highlight["box"]
        x1, y1, x2, y2 = _px(mb, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=6)
        label = highlight.get("label", "MISSING")
        draw.text((x1 + 4, y2 + 4), label, fill=(255, 0, 0))

    # Title bar
    if title:
        draw.rectangle([0, 0, W, 40], fill=(0, 0, 0))
        draw.text((10, 10), title, fill=(255, 255, 255))

    # Save to bytes
    out_buf = io.BytesIO()
    img.save(out_buf, format="PNG")
    out_buf.seek(0)

    s3.put_object(
        Bucket=out_bucket,
        Key=out_key,
        Body=out_buf.getvalue(),
        ContentType="image/png"
    )

    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": out_bucket, "Key": out_key},
        ExpiresIn=PRESIGN_EXPIRES
    )
    return url

def _px(box, W, H):
    x1 = int(box["Left"] * W)
    y1 = int(box["Top"] * H)
    x2 = int((box["Left"] + box["Width"]) * W)
    y2 = int((box["Top"] + box["Height"]) * H)
    return x1, y1, x2, y2

# =========================
# Response helper
# =========================
def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False)
    }
