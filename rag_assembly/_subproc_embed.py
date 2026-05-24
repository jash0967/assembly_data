"""stdin으로 query 받고 stdout으로 JSON vector 반환. MCP에서 subprocess로 호출."""
import json
import sys
import os

import _bootstrap  # noqa: F401
import google.auth
import google.auth.transport.requests
import requests
import config as cfg


def main():
    query = sys.stdin.read().strip()
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    req = google.auth.transport.requests.Request()
    creds.refresh(req)
    url = (f"https://us-central1-aiplatform.googleapis.com/v1beta1/projects/"
           f"{cfg.GCP_PROJECT}/locations/us-central1/publishers/google/models/"
           f"{cfg.EMBED_MODEL}:predict")
    headers = {"Authorization": f"Bearer {creds.token}",
               "Content-Type": "application/json"}
    body = {
        "instances": [{"content": query, "task_type": "RETRIEVAL_QUERY"}],
        "parameters": {"outputDimensionality": cfg.EMBED_DIM},
    }
    r = requests.post(url, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    vec = data["predictions"][0]["embeddings"]["values"]
    sys.stdout.write(json.dumps({"vector": vec}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
