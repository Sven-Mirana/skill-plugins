#!/usr/bin/env bash
set -euo pipefail

VERSION="v5.0.0"
EXPECTED_SHA256="1a13d4ff07526873564c9fa21b15ecdfa87eae8e6a240951bc10367e88f8110a"
EXPECTED_SIZE="498689"
REPOSITORY="Sven-Mirana/sublation"
ASSET="${RELEASE_ASSET:-}"
STATE_FILE="${PUBLICATION_STATE_FILE:-/tmp/sublation-v5-publication-state.json}"
PHASE="PRECHECK"

write_state() {
  local state="$1"
  local exit_code="${2:-0}"
  PUBLICATION_STATE="$state" PUBLICATION_PHASE="$PHASE" PUBLICATION_EXIT_CODE="$exit_code" \
    PUBLICATION_TAG_OBJECT_SHA="${TAG_OBJECT_SHA:-}" PUBLICATION_HEAD_SHA="${HEAD_SHA:-}" \
    PUBLICATION_STATE_FILE="$STATE_FILE" python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

value = {
    "schema_version": "skill-sublation.publication-runtime-state.v1",
    "state": os.environ["PUBLICATION_STATE"],
    "phase": os.environ["PUBLICATION_PHASE"],
    "exit_code": int(os.environ["PUBLICATION_EXIT_CODE"]),
    "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "repository": "Sven-Mirana/sublation",
    "tag": "v5.0.0",
    "asset_name": "skill-sublation-v5.0.0-full-mit-20260825.zip",
    "asset_sha256": "sha256:1a13d4ff07526873564c9fa21b15ecdfa87eae8e6a240951bc10367e88f8110a",
    "tag_object_sha": os.environ.get("PUBLICATION_TAG_OBJECT_SHA") or None,
    "peeled_commit_sha": os.environ.get("PUBLICATION_HEAD_SHA") or None,
}
path = Path(os.environ["PUBLICATION_STATE_FILE"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

on_exit() {
  local exit_code="$?"
  if [[ "$exit_code" -ne 0 ]]; then
    set +e
    write_state "PARTIAL_OR_FAILED" "$exit_code"
    echo "发布未完成；阶段 $PHASE，状态收据：$STATE_FILE" >&2
  fi
}

trap on_exit EXIT

verify_tag() {
  local ref_file="$STATE_FILE.tag-ref.json"
  local object_file="$STATE_FILE.tag-object.json"
  gh api "repos/$REPOSITORY/git/ref/tags/$VERSION" >"$ref_file"
  TAG_OBJECT_SHA="$(
    python3 - "$ref_file" <<'PY'
import json
import sys
from pathlib import Path

ref = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if ref.get("ref") != "refs/tags/v5.0.0":
    raise SystemExit("tag ref name mismatch")
target = ref.get("object") or dict()
if target.get("type") != "tag":
    raise SystemExit("remote tag is not annotated")
sha = target.get("sha")
if not isinstance(sha, str) or len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
    raise SystemExit("invalid annotated tag object sha")
print(sha)
PY
  )"
  gh api "repos/$REPOSITORY/git/tags/$TAG_OBJECT_SHA" >"$object_file"
  EXPECTED_HEAD_SHA="$HEAD_SHA" python3 - "$object_file" <<'PY'
import json
import os
import sys
from pathlib import Path

tag = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if tag.get("tag") != "v5.0.0":
    raise SystemExit("annotated tag name mismatch")
target = tag.get("object") or dict()
if target.get("type") != "commit":
    raise SystemExit("annotated tag does not peel to a commit")
if target.get("sha") != os.environ["EXPECTED_HEAD_SHA"]:
    raise SystemExit("annotated tag peeled commit mismatch")
PY
}

if [[ "${CONFIRM_PUBLISH:-}" != "1" ]]; then
  echo "仅执行发布预览；未创建 tag、GitHub Release 或上传资产。"
  echo "请先检查 RELEASE-v5.0.md、PUBLICATION.json 与校验清单。"
  echo "确认后使用：CONFIRM_PUBLISH=1 RELEASE_ASSET=/path/to/skill-sublation-v5.0.0-full-mit-20260825.zip ./publish.sh"
  exit 0
fi

if [[ -z "$ASSET" || ! -f "$ASSET" ]]; then
  echo "RELEASE_ASSET 必须指向已验证的完整 V5 ZIP。" >&2
  exit 1
fi

test -z "$(git status --porcelain)"
gh auth status >/dev/null
test "$(stat -f '%z' "$ASSET")" = "$EXPECTED_SIZE"
test "$(shasum -a 256 "$ASSET" | awk '{print $1}')" = "$EXPECTED_SHA256"
shasum -a 256 --check SHA256SUMS.txt
shasum -a 256 --check GITHUB-SHA256SUMS.txt
python3 -B scripts/release_audit.py --root . --report /tmp/sublation-release-audit.json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts \
  python3 -B -m unittest discover -s scripts -p 'test_*.py'

if git rev-parse --verify "refs/tags/$VERSION" >/dev/null 2>&1; then
  echo "本地 tag $VERSION 已存在；拒绝覆盖。" >&2
  exit 1
fi

set +e
git ls-remote --exit-code --tags origin "refs/tags/$VERSION" >/dev/null 2>&1
REMOTE_TAG_STATUS="$?"
set -e
if [[ "$REMOTE_TAG_STATUS" -eq 0 ]]; then
  echo "远端 tag $VERSION 已存在；即使 Release 缺失也拒绝复用。" >&2
  exit 1
elif [[ "$REMOTE_TAG_STATUS" -ne 2 ]]; then
  echo "无法可靠读取远端 tag 状态；拒绝继续。" >&2
  exit 1
fi

RELEASE_PROBE="$STATE_FILE.release-precheck.txt"
set +e
gh api -i "repos/$REPOSITORY/releases/tags/$VERSION" >"$RELEASE_PROBE" 2>&1
RELEASE_PROBE_STATUS="$?"
set -e
RELEASE_HTTP_STATUS="$(awk 'NR == 1 {print $2}' "$RELEASE_PROBE")"
if [[ "$RELEASE_PROBE_STATUS" -eq 0 && "$RELEASE_HTTP_STATUS" == "200" ]]; then
  echo "GitHub Release $VERSION 已存在；拒绝覆盖。" >&2
  exit 1
elif [[ "$RELEASE_HTTP_STATUS" != "404" ]]; then
  echo "无法可靠确认 GitHub Release 不存在；HTTP $RELEASE_HTTP_STATUS，拒绝继续。" >&2
  exit 1
fi

HEAD_SHA="$(git rev-parse HEAD)"
REMOTE_MAIN_SHA="$(git ls-remote origin refs/heads/main | awk '{print $1}')"
if [[ "$HEAD_SHA" != "$REMOTE_MAIN_SHA" ]]; then
  echo "当前 HEAD 不等于 origin/main；先通过受审 PR 更新 main。" >&2
  exit 1
fi

PHASE="TAG_PUSH"
git tag -a "$VERSION" -m "Skill Sublation $VERSION"
git push origin "refs/tags/$VERSION"
write_state "TAG_PUSHED_PENDING_RELEASE"

PHASE="TAG_READBACK"
verify_tag
write_state "TAG_READBACK_VERIFIED_PENDING_RELEASE"

PHASE="DRAFT_RELEASE_CREATE"
gh release create "$VERSION" \
  --repo "$REPOSITORY" \
  --verify-tag \
  --draft \
  --title "Skill Sublation v5.0.0" \
  --notes-file RELEASE-v5.0.md

write_state "DRAFT_CREATED_PENDING_ASSET"

PHASE="ASSET_UPLOAD"
gh release upload "$VERSION" \
  "$ASSET#Sublation V5.0 完整 MIT 安装包" \
  --repo "$REPOSITORY"
write_state "ASSET_UPLOADED_PENDING_DRAFT_READBACK"

verify_release() {
  local expected_draft="$1"
  local readback_file="$STATE_FILE.readback.json"
  gh api "repos/$REPOSITORY/releases?per_page=100" >"$readback_file"
  EXPECTED_DRAFT="$expected_draft" EXPECTED_SIZE="$EXPECTED_SIZE" \
    EXPECTED_SHA256="$EXPECTED_SHA256" EXPECTED_ASSET_NAME="skill-sublation-v5.0.0-full-mit-20260825.zip" \
    python3 - "$readback_file" <<'PY'
import json
import os
import sys
from pathlib import Path

releases = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
matches = [release for release in releases if release.get("tag_name") == "v5.0.0"]
if len(matches) != 1:
    raise SystemExit("expected exactly one release for tag")
release = matches[0]
expected_draft = os.environ["EXPECTED_DRAFT"] == "true"
if release.get("tag_name") != "v5.0.0" or release.get("draft") is not expected_draft:
    raise SystemExit("release tag/draft readback mismatch")
assets = [a for a in release.get("assets", []) if a.get("name") == os.environ["EXPECTED_ASSET_NAME"]]
if len(assets) != 1:
    raise SystemExit("expected exactly one release asset")
asset = assets[0]
if asset.get("size") != int(os.environ["EXPECTED_SIZE"]):
    raise SystemExit("release asset size mismatch")
if asset.get("digest") != "sha256:" + os.environ["EXPECTED_SHA256"]:
    raise SystemExit("release asset digest mismatch")
PY
}

PHASE="DRAFT_READBACK"
verify_release true
write_state "DRAFT_READBACK_VERIFIED_PENDING_PUBLISH"

PHASE="RELEASE_PUBLISH"
gh release edit "$VERSION" --repo "$REPOSITORY" --draft=false --latest
write_state "PUBLISH_RETURNED_PENDING_FINAL_READBACK"

PHASE="FINAL_TAG_READBACK"
verify_tag
write_state "FINAL_TAG_READBACK_VERIFIED_PENDING_RELEASE_READBACK"

PHASE="FINAL_RELEASE_READBACK"
verify_release false
write_state "READBACK_VERIFIED"
trap - EXIT
echo "远端 tag、Release 与 asset API 读回均已通过；运行态收据：$STATE_FILE"
