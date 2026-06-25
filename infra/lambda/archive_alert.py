"""
archive_alert.py
Lambda: intellidoc-alert

Publishes SNS notifications for the archive Step Functions workflow.
Used for both per-document failures (DocumentArchiveFailed) and the
workflow-level completion/failure summary (ArchiveComplete, NotifyFailure).

Input event (completion summary):
  { "subject": "IntelliDoc archive run complete", "candidates": { "count": N, "file_ids": [...] } }

Input event (document failure):
  { "subject": "IntelliDoc archive failed", "file_id": "uuid", "error": { ... } }

Input event (workflow failure):
  { "subject": "IntelliDoc archive workflow FAILED", "error": { ... } }

Output:
  { "message_id": "sns-message-id" }

No static AWS keys — Lambda uses its execution role.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SNS_TOPIC_ARN = os.environ.get("SNS_ALERT_TOPIC_ARN", "")
AWS_REGION    = os.environ.get("AWS_REGION", "us-east-1")

_sns = None


def _get_sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", region_name=AWS_REGION)
    return _sns


def build_message(event: dict) -> str:
    subject = event.get("subject", "IntelliDoc notification")

    if "candidates" in event:
        candidates = event["candidates"]
        return (
            f"{subject}\n\n"
            f"Documents processed: {candidates.get('count', 0)}\n"
            f"File IDs: {json.dumps(candidates.get('file_ids', []), indent=2)}"
        )

    if "file_id" in event:
        error = event.get("error", {})
        return (
            f"{subject}\n\n"
            f"File ID: {event['file_id']}\n"
            f"Error: {json.dumps(error, indent=2)}"
        )

    if "error" in event:
        return (
            f"{subject}\n\n"
            f"Error: {json.dumps(event['error'], indent=2)}"
        )

    return f"{subject}\n\n{json.dumps(event, indent=2)}"


def lambda_handler(event: dict, context) -> dict:
    subject = event.get("subject", "IntelliDoc notification")
    message = build_message(event)

    logger.info("Publishing alert: %s", subject)

    if not SNS_TOPIC_ARN:
        logger.warning("SNS_ALERT_TOPIC_ARN not set — alert not sent: %s", subject)
        return {"message_id": None}

    resp = _get_sns().publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject[:100],
        Message=message,
    )
    message_id = resp["MessageId"]
    logger.info("SNS published: %s", message_id)
    return {"message_id": message_id}


if __name__ == "__main__":
    result = lambda_handler({
        "subject": "IntelliDoc archive run complete",
        "candidates": {"count": 3, "file_ids": ["uuid-1", "uuid-2", "uuid-3"]},
    }, None)
    print(json.dumps(result, indent=2))
