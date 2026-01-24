import json
import boto3
import os
from datetime import datetime, timezone
from decimal import Decimal
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

EVENTS_TABLE_NAME = os.getenv("EVENTS_TABLE_NAME", "LifeShot_Events")

# Prefer FRAMES_BUCKET; fallback to IMAGES_BUCKET; fallback default
FRAMES_BUCKET_ENV = os.getenv("FRAMES_BUCKET", os.getenv("IMAGES_BUCKET", "lifeshot-pool-images")).strip()

PRESIGN_EXPIRES = int(os.getenv("PRESIGN_EXPIRES", "900"))


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def _cors_headers():
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET,PATCH,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
    }


def _response(code, body_obj):
    return {
        "statusCode": code,
        "headers": _cors_headers(),
        "body": json.dumps(body_obj, cls=DecimalEncoder),
    }


def _presign_get(bucket, key):
    if not key or not bucket:
        return None
    try:
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_EXPIRES,
        )
    except ClientError:
        return None


def _iso_utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_bucket_for_event(item: dict) -> str:
    """
    Priority:
    1) item['bucket'] if present
    2) FRAMES_BUCKET env
    3) fallback name
    """
    b = (item.get("bucket") or "").strip()
    if b:
        return b
    return FRAMES_BUCKET_ENV or "lifeshot-pool-images"


def _claims_from_event(event: dict) -> dict:
    """
    Works with API Gateway v2 HTTP API + JWT authorizer:
      event['requestContext']['authorizer']['jwt']['claims']
    """
    try:
        return (
            event.get("requestContext", {})
            .get("authorizer", {})
            .get("jwt", {})
            .get("claims", {})
        ) or {}
    except Exception:
        return {}


def _is_authenticated(event: dict) -> bool:
    """
    We rely on API Gateway JWT Authorizer to validate token.
    If request reached Lambda and claims exist -> authenticated.
    """
    claims = _claims_from_event(event)
    return bool(claims)  # usually contains sub, iss, client_id, token_use, etc.


def lambda_handler(event, context):
    path = event.get("rawPath") or event.get("path") or ""
    if not path and "requestContext" in event:
        path = event["requestContext"].get("http", {}).get("path", "")

    method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "")
    ).upper()

    # Preflight
    if method == "OPTIONS":
        return {"statusCode": 200, "headers": _cors_headers(), "body": ""}

    # Route guard
    if "events" not in path:
        return _response(404, {"error": "Not found"})

    # Require authenticated (JWT Authorizer should enforce anyway)
    if not _is_authenticated(event):
        return _response(401, {"error": "Unauthorized"})

    table = dynamodb.Table(EVENTS_TABLE_NAME)

    # ==========================
    # PATCH /events  (Close event) - allowed for Lifeguard + Admin
    # ==========================
    if method == "PATCH":
        try:
            body = json.loads(event.get("body", "{}") or "{}")
            event_id = body.get("eventId")
            if not event_id:
                return _response(400, {"error": "eventId is required"})

            get_resp = table.get_item(Key={"eventId": event_id})
            item = get_resp.get("Item")
            if not item:
                return _response(404, {"error": "Event not found"})

            # Already closed? keep idempotent behavior
            if str(item.get("status", "")).upper() == "CLOSED":
                return _response(
                    200,
                    {
                        "message": "Already closed",
                        "eventId": event_id,
                        "closedAt": item.get("closedAt"),
                        "responseSeconds": item.get("responseSeconds", -1),
                    },
                )

            created_at = item.get("created_at")
            closed_at = _iso_utc_now()

            response_seconds = -1
            try:
                if created_at:
                    created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                    response_seconds = int((closed_dt - created_dt).total_seconds())
            except Exception:
                response_seconds = -1

            table.update_item(
                Key={"eventId": event_id},
                UpdateExpression="SET #s = :s, closedAt = :c, responseSeconds = :r",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "CLOSED",
                    ":c": closed_at,
                    ":r": response_seconds,
                },
            )

            return _response(
                200,
                {
                    "message": "Closed",
                    "eventId": event_id,
                    "closedAt": closed_at,
                    "responseSeconds": response_seconds,
                },
            )

        except Exception as e:
            return _response(500, {"error": "PATCH failed", "details": str(e)})

    # ==========================
    # GET /events (Any authenticated user)
    # ==========================
    if method == "GET":
        try:
            resp = table.scan()
            items = resp.get("Items", [])

            while "LastEvaluatedKey" in resp:
                resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                items.extend(resp.get("Items", []))

            for it in items:
                bucket = _resolve_bucket_for_event(it)

                prev_key = it.get("prevImageKey")
                warn_key = it.get("warningImageKey")

                it["bucketResolved"] = bucket
                it["prevImageUrl"] = _presign_get(bucket, prev_key)
                it["warningImageUrl"] = _presign_get(bucket, warn_key)

            return _response(200, items)

        except Exception as e:
            return _response(500, {"error": "GET events failed", "details": str(e)})

    return _response(405, {"error": "Method not allowed"})
