import json
import boto3
import os
from decimal import Decimal
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

# ---- Config (ENV with defaults) ----
EVENTS_TABLE_NAME = os.getenv("EVENTS_TABLE_NAME", "LifeShot_Events")
IMAGES_BUCKET     = os.getenv("IMAGES_BUCKET", "lifeshot-pool-images")
PRESIGN_EXPIRES   = int(os.getenv("PRESIGN_EXPIRES", "900"))  # 15 minutes


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)


def _cors_headers():
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,PATCH,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


def _response(code, body_obj):
    return {
        "statusCode": code,
        "headers": _cors_headers(),
        "body": json.dumps(body_obj, cls=DecimalEncoder),
    }


def _presign_get(bucket, key):
    if not key:
        return None
    try:
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_EXPIRES,
        )
    except ClientError:
        return None


def _pick_first(item, keys):
    """Return first existing non-empty key value from item."""
    for k in keys:
        v = item.get(k)
        if v:
            return v
    return None


def lambda_handler(event, context):
    # Resolve path + method for both REST API & HTTP API formats
    path = event.get("rawPath") or event.get("path") or ""
    if not path and "requestContext" in event:
        path = event["requestContext"].get("http", {}).get("path", "")

    method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "")
    ).upper()

    # CORS preflight
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": _cors_headers(), "body": ""}

    # =========================
    # /events endpoint
    # =========================
    if "events" in path:
        table = dynamodb.Table(EVENTS_TABLE_NAME)

        # ---- PATCH: close event ----
        if method == "PATCH":
            try:
                body = json.loads(event.get("body", "{}") or "{}")
                event_id = body.get("eventId")
                if not event_id:
                    return _response(400, {"error": "eventId is required"})

                table.update_item(
                    Key={"eventId": event_id},
                    UpdateExpression="set #s = :s",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":s": "CLOSED"},
                )
                return _response(200, {"message": "Closed"})
            except Exception as e:
                return _response(500, {"error": "PATCH failed", "details": str(e)})

        # ---- GET: list events + add presigned URLs for prev+warning ----
        if method == "GET":
            try:
                resp = table.scan()
                items = resp.get("Items", [])

                # pagination
                while "LastEvaluatedKey" in resp:
                    resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                    items.extend(resp.get("Items", []))

                # Add URLs
                for it in items:
                    # Prefer the new field names, but allow legacy ones too
                    prev_key = _pick_first(it, ["prevImageKey", "prev_image_key", "prev_key"])
                    warn_key = _pick_first(it, ["warningImageKey", "warning_image_key", "warning_key"])

                    # These keys SHOULD point to TestingSet if your "create event" lambda saves them that way
                    it["prevImageUrl"] = _presign_get(IMAGES_BUCKET, prev_key)
                    it["warningImageUrl"] = _presign_get(IMAGES_BUCKET, warn_key)

                return _response(200, items)

            except Exception as e:
                return _response(500, {"error": "GET events failed", "details": str(e)})

        return _response(405, {"error": "Method not allowed"})

    # =========================
    # Default (אם אתה כבר לא צריך upload_url אפשר למחוק את החלק הזה)
    # =========================
    file_name = "test.jpg"
    upload_url = s3_client.generate_presigned_url(
        "put_object",
        Params={"Bucket": IMAGES_BUCKET, "Key": file_name},
        ExpiresIn=3600,
    )
    return _response(200, {"upload_url": upload_url, "file_name": file_name})
