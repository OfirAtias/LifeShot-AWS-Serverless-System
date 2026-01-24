"""Render-and-upload Lambda.

Cosmetic refactor only:
- Improves readability via spacing, section headers, and comments.
- Does not change functionality or behavior.
"""

import boto3
import json
import io
import os
from PIL import Image, ImageDraw
from botocore.exceptions import ClientError

 
# =============================================================================
# AWS clients
# =============================================================================
s3 = boto3.client("s3")

 
# =============================================================================
# Environment configuration
# =============================================================================
DEFAULT_PRESIGN_EXPIRES = int(os.getenv("PRESIGN_EXPIRES", "3600"))


# =============================================================================
# Geometry helpers
# =============================================================================


# Convert a Rekognition-style bounding box (normalized floats) into pixel coords.
def _px(box, W, H):
    x1 = int(float(box["Left"]) * W)
    y1 = int(float(box["Top"]) * H)
    x2 = int((float(box["Left"]) + float(box["Width"])) * W)
    y2 = int((float(box["Top"]) + float(box["Height"])) * H)
    return x1, y1, x2, y2


# =============================================================================
# S3 helpers
# =============================================================================


# Create a pre-signed S3 GET URL (or None on failure).
def presign_get_url(bucket, key, expires):
    try:
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )
    except ClientError:
        return None


# =============================================================================
# Rendering
# =============================================================================


# Download an image from S3, draw annotations, and return PNG bytes.
def render_png(src_bucket, src_key, title, curr_boxes, missing_boxes):
    img_bytes = s3.get_object(Bucket=src_bucket, Key=src_key)["Body"].read()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size

    # Title bar
    draw.rectangle([0, 0, W, 58], fill=(0, 0, 0))
    draw.text((12, 18), title, fill=(255, 255, 255))

    # GREEN boxes
    for b in (curr_boxes or []):
        x1, y1, x2, y2 = _px(b, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=4)
        draw.text((x1 + 6, max(62, y1 - 18)), "PERSON", fill=(0, 255, 0))

    # RED missing boxes
    for mb in (missing_boxes or []):
        x1, y1, x2, y2 = _px(mb, W, H)
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=7)
        draw.text((x1 + 6, max(62, y1 - 18)), "POSSIBLE DROWNING!", fill=(255, 0, 0))

    out_buf = io.BytesIO()
    img.save(out_buf, format="PNG")
    out_buf.seek(0)
    return out_buf.getvalue()


# =============================================================================
# Lambda handler
# =============================================================================


# Expected payload:
# {
#   "bucket": "...",
#   "src_key": "...",
#   "out_key": "...",
#   "title": "...",
#   "curr_boxes": [...],
#   "missing_boxes": [...],
#   "presign_expires": 3600
# }
def lambda_handler(event, context):
    # expected payload:
    # {
    #   "bucket": "...",
    #   "src_key": "...",
    #   "out_key": "...",
    #   "title": "...",
    #   "curr_boxes": [...],
    #   "missing_boxes": [...],
    #   "presign_expires": 3600
    # }
    try:
        bucket = event.get("bucket")
        src_key = event.get("src_key")
        out_key = event.get("out_key")
        title = event.get("title", "")
        curr_boxes = event.get("curr_boxes", [])
        missing_boxes = event.get("missing_boxes", [])
        expires = int(event.get("presign_expires", DEFAULT_PRESIGN_EXPIRES))

        if not bucket or not src_key or not out_key:
            return {"ok": False, "error": "bucket/src_key/out_key required"}

        png_bytes = render_png(bucket, src_key, title, curr_boxes, missing_boxes)
        s3.put_object(
            Bucket=bucket, Key=out_key, Body=png_bytes, ContentType="image/png"
        )

        out_url = presign_get_url(bucket, out_key, expires)
        return {"ok": True, "out_key": out_key, "out_url": out_url}

    except Exception as e:
        return {"ok": False, "error": str(e)}
