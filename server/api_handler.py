import json
import boto3
import os
from decimal import Decimal
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
s3_client = boto3.client('s3')

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
        'Content-Type': 'application/json',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'GET,PATCH,OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type',
    }


def _response(code, body_obj):
    return {
        'statusCode': code,
        'headers': _cors_headers(),
        'body': json.dumps(body_obj, cls=DecimalEncoder)
    }


def lambda_handler(event, context):
    # Resolve path + method for both REST API & HTTP API formats
    path = event.get('rawPath') or event.get('path') or ''
    if not path and 'requestContext' in event:
        path = event['requestContext'].get('http', {}).get('path', '')

    method = (
        event.get('httpMethod')
        or event.get('requestContext', {}).get('http', {}).get('method', '')
    ).upper()

    # Handle CORS preflight
    if method == "OPTIONS":
        return {
            'statusCode': 200,
            'headers': _cors_headers(),
            'body': ''
        }

    # =========================
    # /events endpoint
    # =========================
    if 'events' in path:
        table = dynamodb.Table(EVENTS_TABLE_NAME)

        # ---- PATCH: close event ----
        if method == 'PATCH':
            try:
                body = json.loads(event.get('body', '{}') or '{}')
                event_id = body.get('eventId')

                if not event_id:
                    return _response(400, {'error': 'eventId is required'})

                table.update_item(
                    Key={'eventId': event_id},
                    UpdateExpression="set #s = :s",
                    ExpressionAttributeNames={'#s': 'status'},
                    ExpressionAttributeValues={':s': 'CLOSED'}
                )

                return _response(200, {'message': 'Closed'})
            except Exception as e:
                return _response(500, {'error': 'PATCH failed', 'details': str(e)})

        # ---- GET: list events + add warningImageUrl ----
        try:
            response = table.scan()
            items = response.get('Items', [])

            # Pagination
            while "LastEvaluatedKey" in response:
                response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
                items.extend(response.get('Items', []))

            # Add warningImageUrl if warningImageKey exists
            for it in items:
                key = it.get("warningImageKey") or it.get("warning_key")
                if key:
                    try:
                        it["warningImageUrl"] = s3_client.generate_presigned_url(
                            'get_object',
                            Params={'Bucket': IMAGES_BUCKET, 'Key': key},
                            ExpiresIn=PRESIGN_EXPIRES
                        )
                    except ClientError:
                        it["warningImageUrl"] = None
                else:
                    it["warningImageUrl"] = None

            return _response(200, items)

        except Exception as e:
            return _response(500, {'error': 'GET events failed', 'details': str(e)})

    # =========================
    # Default / upload-url example (keep if you still use it)
    # =========================
    bucket_name = IMAGES_BUCKET
    file_name = 'test.jpg'

    upload_url = s3_client.generate_presigned_url(
        'put_object',
        Params={'Bucket': bucket_name, 'Key': file_name},
        ExpiresIn=3600
    )

    return _response(200, {'upload_url': upload_url, 'file_name': file_name})
