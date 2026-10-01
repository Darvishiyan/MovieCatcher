#!/bin/sh
# Run from cron after SubDL's daily UTC quota reset. Pass library roots inside the container.
set -eu

container=${MOVIECATCHER_CONTAINER:-moviecatcher-moviecatcher-1}
if [ "$#" -eq 0 ]; then
    echo 'Usage: retry-subtitles.sh /media/movies [/media/series ...]' >&2
    exit 2
fi

for library in "$@"; do
    echo "Scanning $library at $(date -Is)"
    if docker exec "$container" python /app/subtitles.py scan "$library"; then
        :
    else
        result=$?
        if [ "$result" -eq 75 ]; then
            echo 'SubDL quota reached; remaining files will be retried on the next scheduled run.'
        fi
        exit "$result"
    fi
done
