import boto3
import json
import os
import re
import math
from datetime import datetime
from PIL import Image, ImageDraw
import io

# =========================
# CONFIG (Env Vars)
# =========================
FRAMES_BUCKET   = os.getenv("FRAMES_BUCKET", "lifeshot-pool-images")
FRAMES_PREFIX   = os.getenv("FRAMES_PREFIX", "LifeShot/")   # where 1.png.. are
OUT_PREFIX      = os.getenv("OUT_PREFIX", "LifeShot/Warnings/")

MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "70"))
MAX_FRAMES      = int(os.getenv("MAX_FRAMES", "50"))

# "count drop" rule
DROP_BY         = int(os.getenv("DROP_BY", "1"))            # ירידה ב-1 = אזהרה
CONFIRM_FRAMES  = int(os.getenv("CONFIRM_FRAMES", "1"))     # כמה פריימים רצופים של ירידה צריך

# Filter boxes (normalized area)
MIN_BOX_AREA    = float(os.getenv("MIN_BOX_AREA", "0.002"))
MAX_BOX_AREA    = float(os.getenv("MAX_BOX_AREA", "0.70"))

# Matching between frames (to decide *which* box disappeared)
MATCH_IOU_MIN   = float(os.getenv("MATCH_IOU_MIN", "0.08"))
MATCH_CENTER_MAX= float(os.getenv("MATCH_CENTER_MAX", "0.12"))  # normalized distance

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

    # 2) iterate frames + detect count drop
    alerts = []
    counts = []

    prev_key = None
    prev_boxes = None
    prev_count = None

    drop_streak = 0
    last_drop_info = None  # (missing_on_key, last_seen_key, prev_boxes, curr_boxes)

    for key in frame_keys:
        boxes = detect_person_boxes(FRAMES_BUCKET, key)
        curr_count = len(boxes)
        counts.append({"key": key, "count": curr_count})

        if prev_boxes is not None:
            # count drop condition
            if curr_count <= (prev_count - DROP_BY):
                drop_streak += 1
                last_drop_info = (key, prev_key, prev_boxes, boxes, prev_count, curr_count)
            else:
                drop_streak = 0
                last_drop_info = None

            # confirm
            if drop_streak >= CONFIRM_FRAMES and last_drop_info is not None:
                missing_on_key, last_seen_key, last_seen_boxes, curr_boxes, pc, cc = last_drop_info

                # find which boxes from last_seen are missing now
                missing_prev_idxs = find_missing(last_seen_boxes, curr_boxes)

                # create warning image ONLY now
                # If multiple missing, generate one warning per missing box (or bundle them)
                # We'll bundle all missing boxes in one image to reduce S3 spam.
                missing_boxes = [last_seen_boxes[i] for i in missing_prev_idxs]

                warning_key = f"{OUT_PREFIX}{_basename(last_seen_key)}_WARNING.png"
                warning_url = create_warning_image(
                    src_bucket=FRAMES_BUCKET,
                    src_key=last_seen_key,
                    out_bucket=FRAMES_BUCKET,
                    out_key=warning_key,
                    all_boxes=last_seen_boxes,
                    missing_boxes=missing_boxes,
                    title=f"WARNING: count drop | last_seen={last_seen_key} -> missing_on={missing_on_key} | {pc}->{cc}"
                )

                alerts.append({
                    "missing_detected_on_frame": missing_on_key,
                    "last_seen_frame": last_seen_key,
                    "prev_count": pc,
                    "curr_count": cc,
                    "missing_count_estimated": len(missing_boxes),
                    "missing_boxes_last_seen": missing_boxes,
                    "warning_image_key": warning_key,
                    "warning_image_url": warning_url,
                })

                # after alert, reset streak so we don't spam every next frame
                drop_streak = 0
                last_drop_info = None

        prev_key = key
        prev_boxes = boxes
        prev_count = curr_count

    return _resp(200, {
        "status": "WARNINGS_CREATED" if alerts else "NO_ALERT",
        "drop_by": DROP_BY,
        "confirm_frames": CONFIRM_FRAMES,
        "total_frames": len(frame_keys),
        "alerts_count": len(alerts),
        "alerts": alerts,
        "counts": counts
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
    name = key.split("/")[-1]
    return name.rsplit(".", 1)[0]


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
# Create warning image (only when drop happens)
# =========================
def create_warning_image(src_bucket, src_key, out_bucket, out_key, all_boxes, missing_boxes, title):
    img_bytes = s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # title bar
    draw.rectangle([0, 0, W, 44], fill=(0, 0, 0))
    draw.text((10, 12), title, fill=(255, 255, 255))

    # draw all people in green
    for b in all_boxes:
        x1, y1, x2, y2 = _px(b, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=4)

    # highlight missing in red + label
    for mb in missing_boxes:
        x1, y1, x2, y2 = _px(mb, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=6)
        draw.text((x1 + 4, y2 + 4), "LAST SEEN", fill=(255, 0, 0))

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
