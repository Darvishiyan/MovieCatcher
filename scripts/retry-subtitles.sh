#!/bin/sh
# Run from cron after SubDL's daily UTC quota reset. Pass library roots inside the container.
set -eu

container=${MOVIECATCHER_CONTAINER:-moviecatcher-moviecatcher-1}
if [ "$#" -eq 0 ]; then
    echo 'Usage: retry-subtitles.sh /media/movies [/media/series ...]' >&2
    exit 2
fi

write_inventory() {
    docker exec "$container" python /app/subtitle_inventory.py \
        --output /data/subtitle-inventory-latest.json "$@" || \
        echo 'Could not write the latest subtitle inventory.' >&2
}
trap 'write_inventory "$@"' EXIT

for library in "$@"; do
    echo "Reconciling English subtitles in $library at $(date -Is)"
    if ! docker exec "$container" python /app/subtitle_normalize.py normalize "$library"; then
        echo "Some subtitle duplicates need manual review in $library; see the persistent error log."
    fi
done

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
