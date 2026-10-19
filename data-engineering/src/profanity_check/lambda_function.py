from helpers import get_profanity_score, insert_into_request, insert_into_fraud_requests


def lambda_handler(event, context=None):
    request_data = event
    score = get_profanity_score(request_data["req_desc"])

    if score == 0:
        insert_into_request(request_data)
        return {"req_id": request_data["req_id"], "score": score, "routed_to": "request"}
    else:
        insert_into_fraud_requests(request_data, score)
        return {"req_id": request_data["req_id"], "score": score, "routed_to": "fraud_requests"}


if __name__ == "__main__":
    sample_requests = [
        {"req_id": "REQ-001", "creator_id": "SID-00-000-000-000-000-001", "req_desc": "Can someone help me carry groceries?"},
        {"req_id": "REQ-002", "creator_id": "SID-00-000-000-000-000-002", "req_desc": "I need a ride to the clinic tomorrow."},
        {"req_id": "REQ-003", "creator_id": "SID-00-000-000-000-000-003", "req_desc": "Sample text to test flagged routing."},
    ]
    for req in sample_requests:
        print(lambda_handler(req))