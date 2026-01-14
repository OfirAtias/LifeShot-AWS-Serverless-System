import boto3
import json
import os

dynamodb = boto3.resource("dynamodb")
sns = boto3.client("sns")


def publish_sns(topic_arn, subject, message):
    if not topic_arn:
        print("[SNS] sns_topic_arn empty -> skip")
        return
    sns.publish(TopicArn=topic_arn, Subject=subject, Message=message)


def lambda_handler(event, context):
    # expected payload:
    # {
    #   "eventId": "...",
    #   "created_at": "...Z",
    #   "status": "OPEN",
    #   "events_table": "LifeShot_Events",
    #   "sns_topic_arn": "...",
    #   "bucket": "...",
    #   "warningImageKey": "...",
    #   "warningImageUrl": "...",
    #   "prevImageKey": "...",
    #   "prevImageUrl": "..."
    # }

    try:
        event_id = event.get("eventId")
        created_at = event.get("created_at")
        status = event.get("status", "OPEN")

        table_name = event.get("events_table") or os.getenv("EVENTS_TABLE_NAME", "LifeShot_Events")
        topic_arn = event.get("sns_topic_arn") or os.getenv("SNS_TOPIC_ARN", "")

        prev_key = event.get("prevImageKey")
        warn_key = event.get("warningImageKey")
        prev_url = event.get("prevImageUrl")
        warn_url = event.get("warningImageUrl")

        if not event_id or not created_at or not warn_key:
            return {"ok": False, "error": "eventId/created_at/warningImageKey required"}

        table = dynamodb.Table(table_name)

        item = {
            "eventId": event_id,
            "status": status,
            "created_at": created_at,
            "warningImageKey": warn_key,
        }
        if prev_key:
            item["prevImageKey"] = prev_key

        table.put_item(Item=item)
        print(f"[DB] Event created: {event_id}")

        subject = f"LifeShot ALERT: {event_id}"
        lines = [
            "🚨 POSSIBLE DROWNING DETECTED",
            "",
            f"EventId: {event_id}",
            f"CreatedAt: {created_at}",
            "",
            "BEFORE (prev):",
            f"PrevImageKey: {prev_key or 'N/A'}",
            f"PrevImageUrl: {prev_url or 'N/A'}",
            "",
            "AFTER (alert):",
            f"WarningImageKey: {warn_key or 'N/A'}",
            f"WarningImageUrl: {warn_url or 'N/A'}",
            "",
            "Open your dashboard to view full details.",
        ]
        msg = "\n".join(lines)

        publish_sns(topic_arn, subject, msg)
        print(f"[SNS] Published alert for {event_id}")

        return {"ok": True}

    except Exception as e:
        return {"ok": False, "error": str(e)}
