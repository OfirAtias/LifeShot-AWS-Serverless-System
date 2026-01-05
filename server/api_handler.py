import json
import boto3
import os
from decimal import Decimal
from datetime import datetime, timezone
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


def _iso_utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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

        # ---- PATCH: close event + save closedAt + responseSeconds ----
        if method == "PATCH":
            try:
                body = json.loads(event.get("body", "{}") or "{}")
                event_id = body.get("eventId")
                if not event_id:
                    return _response(400, {"error": "eventId is required"})

                # read existing item to calculate duration
                get_resp = table.get_item(Key={"eventId": event_id})
                item = get_resp.get("Item")

                if not item:
                    return _response(404, {"error": "Event not found"})

                created_at = item.get("created_at")
                closed_at = _iso_utc_now()

                response_seconds = None
                try:
                    if created_at:
                        # created_at format: 2026-01-05T04:12:00Z
                        created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                        response_seconds = int((closed_dt - created_dt).total_seconds())
                except Exception:
                    response_seconds = None

                # write status + closedAt + responseSeconds
                table.update_item(
                    Key={"eventId": event_id},
                    UpdateExpression="SET #s = :s, closedAt = :c, responseSeconds = :r",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":s": "CLOSED",
                        ":c": closed_at,
                        ":r": response_seconds if response_seconds is not None else -1
                    },
                )

                return _response(200, {
                    "message": "Closed",
                    "eventId": event_id,
                    "closedAt": closed_at,
                    "responseSeconds": response_seconds
                })

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

                # Add URLs + (optional) ensure responseSeconds exists for closed events
                for it in items:
                    prev_key = _pick_first(it, ["prevImageKey", "prev_image_key", "prev_key"])
                    warn_key = _pick_first(it, ["warningImageKey", "warning_image_key", "warning_key"])

                    it["prevImageUrl"] = _presign_get(IMAGES_BUCKET, prev_key)
                    it["warningImageUrl"] = _presign_get(IMAGES_BUCKET, warn_key)

                    # If client wants seconds, it's best to rely on responseSeconds stored on close.
                    # But if it's missing and we do have created_at + closedAt, compute it here too.
                    if it.get("status", "").upper() == "CLOSED":
                        if it.get("responseSeconds") is None:
                            try:
                                ca = it.get("created_at")
                                cl = it.get("closedAt")
                                if ca and cl:
                                    ca_dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                                    cl_dt = datetime.fromisoformat(cl.replace("Z", "+00:00"))
                                    it["responseSeconds"] = int((cl_dt - ca_dt).total_seconds())
                            except Exception:
                                it["responseSeconds"] = -1

                return _response(200, items)

            except Exception as e:
                return _response(500, {"error": "GET events failed", "details": str(e)})

        return _response(405, {"error": "Method not allowed"})
