"""Web Push delivery (pywebpush) + pruning of dead subscriptions."""
import json
import logging

from pywebpush import WebPushException, webpush

import config
import db

log = logging.getLogger("sentinel.notify")


def send_push(title: str, body: str, url: str, tag: str | None = None) -> int:
    if not config.VAPID_PRIVATE_KEY:
        log.info("VAPID_PRIVATE_KEY missing, push disabled (notif: %s)", title)
        return 0
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    sent = 0
    for sub in db.subscriptions():
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": json.loads(sub["keys_json"]),
                },
                data=payload,
                vapid_private_key=config.VAPID_PRIVATE_KEY,
                # fresh dict on every call: pywebpush mutates it (adds aud/exp)
                vapid_claims={"sub": f"mailto:{config.VAPID_CLAIM_EMAIL}"},
            )
            sent += 1
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                log.info("dead subscription (%s), pruning", status)
                db.remove_subscription(sub["endpoint"])
            else:
                log.warning("push failed (%s): %s", status, e)
    return sent
