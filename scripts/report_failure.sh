#!/usr/bin/env bash
# Open one "hata" issue per workflow; later failures of the same workflow add a comment to it
# instead of opening a new issue every day (so a long outage is one notification thread, not 100).
# Usage: scripts/report_failure.sh "<workflow name>" "<run url>"
set -u
WF="$1"; URL="$2"
export TITLE="⚠️ Otomatik çalışma hata verdi: $WF"
gh label create hata --color b60205 --repo "$GITHUB_REPOSITORY" 2>/dev/null || true
NUM=$(gh issue list --repo "$GITHUB_REPOSITORY" --label hata --state open --limit 100 \
        --json number,title --jq 'map(select(.title == env.TITLE)) | .[0].number // empty' || true)
if [ -n "$NUM" ]; then
  gh issue comment "$NUM" --repo "$GITHUB_REPOSITORY" --body "Yine hata verdi: $URL"
else
  gh issue create --repo "$GITHUB_REPOSITORY" --label hata --title "$TITLE" \
    --body "Çalışma kaydı: $URL — Sistem bir sonraki çalışmada kendiliğinden tekrar dener. Aynı hata tekrar ederse bu konuya yorum eklenir; sorun çözülünce bu konuyu kapatın."
fi
