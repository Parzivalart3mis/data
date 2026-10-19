import random
from datetime import datetime

try:
    import psycopg2
except ImportError:
    psycopg2 = None

SCORE_REASONS = {
    1: "Foul language",
    2: "Depressive or suicidal language",
    3: "Threatening language",
}


def get_profanity_score(request_text):
    """
    PLACEHOLDER — stands in for the real Profanity API until the endpoint
    is confirmed by the issue owner (#422). Swap for a real HTTP call later.
    """
    return random.randint(0, 3)


def insert_into_request(request_data):
    """
    PLACEHOLDER — assumes a plain insert into `request` for score=0, since
    no existing matching function was found in the repo. Confirm with
    issue owner if this assumption is wrong.
    """
    print(f"[request table] Inserting req_id={request_data['req_id']} (clean, score 0)")


def insert_into_fraud_requests(request_data, score):
    """
    PLACEHOLDER — fraud_requests columns: fraud_request_id, user_id,
    request_datetime, reason. Assumes `reason` holds a text description
    (not a raw score). Confirm with issue owner if a numeric value is
    preferred instead.
    """
    reason = SCORE_REASONS.get(score, "Unknown")
    print(f"[fraud_requests table] user_id={request_data['creator_id']}, "
          f"request_datetime={datetime.utcnow()}, reason={reason}")