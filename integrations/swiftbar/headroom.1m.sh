#!/bin/sh
# SwiftBar display client for headroom. Remote use assumes an SSH-forwarded
# loopback dashboard; fetched bytes are printed only after version validation.

sentinel='headroom_widget_txt@1'
dashboard='http://127.0.0.1:8377/'

offline() {
    printf '%s\n' \
        'hr OFFLINE | color=gray' \
        '---' \
        'Headroom feed unavailable | color=gray' \
        'Refresh | refresh=true' \
        "Open dashboard | href=$dashboard"
}

tmp=$(mktemp "${TMPDIR:-/tmp}/headroom-swiftbar.XXXXXX") || {
    offline
    exit 0
}
trap 'rm -f "$tmp"' EXIT HUP INT TERM

if [ -n "${HEADROOM_WIDGET_URL:-}" ]; then
    # Accept only an explicit canonical loopback origin (or its widget path).
    raw=${HEADROOM_WIDGET_URL%/}
    case "$raw" in
        http://127.0.0.1:*|http://localhost:*) ;;
        *) offline; exit 0 ;;
    esac
    rest=${raw#http://}
    rest=${rest#*:}
    case "$rest" in
        */widget.txt) port=${rest%/widget.txt} ;;
        *) port=$rest ;;
    esac
    if ! awk -v port="$port" 'BEGIN {
        exit !(port ~ /^[1-9][0-9]*$/ && length(port) <= 5 && port + 0 <= 65535)
    }'
    then
        offline
        exit 0
    fi
    dashboard="http://127.0.0.1:$port/"
    url="${dashboard}widget.txt"
    if ! curl --fail --silent --max-time 3 \
        --max-filesize 65536 --output "$tmp" -- "$url"
    then
        offline
        exit 0
    fi
else
    # HEADROOM_BIN overrides for nonstandard installs; a local binary the
    # user configured, never fetched content
    if ! ${HEADROOM_BIN:-headroom} widget-feed --swiftbar >"$tmp" 2>/dev/null
    then
        offline
        exit 0
    fi
fi

bytes=$(wc -c <"$tmp" | tr -d ' ')
lines=$(wc -l <"$tmp" | tr -d ' ')
IFS= read -r first <"$tmp"
if [ "$bytes" -gt 65536 ] || [ "$lines" -lt 2 ] || [ "$first" != "$sentinel" ]
then
    offline
    exit 0
fi

if ! awk -v sentinel="$sentinel" -v dashboard="$dashboard" '
    NR == 1 { if ($0 != sentinel) bad = 1; next }
    $0 == "---" { next }
    {
        marker = index($0, " | ")
        if (!marker) { bad = 1; next }
        label = substr($0, 1, marker - 1)
        param = substr($0, marker + 3)
        if (index(label, "|") || index(param, "|")) { bad = 1; next }
        if (param ~ /^color=(gray|green|orange|red|yellow)$/) { colors++; next }
        if (param == "refresh=true") { refreshes++; next }
        if (param == "href=" dashboard) { hrefs++; next }
        bad = 1
    }
    END { exit (bad || colors < 1 || refreshes != 1 || hrefs != 1) }
' "$tmp"
then
    offline
    exit 0
fi

sed '1d' "$tmp"
