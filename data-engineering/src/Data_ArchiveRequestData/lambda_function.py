
import json
import logging

from helpers import archive_requests

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """Run the enriched request archival process."""
    try:
        result = archive_requests()

        logger.info(
            "Archival completed: %s",
            json.dumps(result, default=str),
        )

        return {
            "statusCode": 200,
            "body": json.dumps(result, default=str),
        }

    except Exception:
        logger.exception("Request archival failed")

        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "error",
                "message": "Request archival failed",
            }),
        }
