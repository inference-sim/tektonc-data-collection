#!/bin/bash
# run-campaign.sh — crash-resilient wrapper
# Usage: ./blis-campaign/run-campaign.sh --campaign campaign/ --hw H100
#
# Exit codes from the Python runner:
#   0 = success (campaign completed)
#   1 = unrecoverable error (bad config, pre-flight failure)
#   other = crash (restart)

MAX_RESTARTS=3
COUNT=0

while [ $COUNT -lt $MAX_RESTARTS ]; do
    echo "[$(date)] Starting campaign runner (attempt $((COUNT+1))/$MAX_RESTARTS)"
    python blis-campaign run "$@"
    EXIT=$?
    if [ $EXIT -eq 0 ]; then
        echo "[$(date)] Campaign completed successfully."
        exit 0
    fi
    if [ $EXIT -eq 1 ]; then
        echo "[$(date)] Unrecoverable error (exit 1). Check logs."
        exit 1
    fi
    COUNT=$((COUNT+1))
    echo "[$(date)] Runner crashed (exit $EXIT). Restart $COUNT/$MAX_RESTARTS in 30s..."
    sleep 30
done

echo "[$(date)] FATAL: Campaign runner crashed $MAX_RESTARTS times. Giving up."
exit 2
