#!/usr/bin/env bash
# Publish all generated branches together, without rewriting their history.
set -euo pipefail

checkout=$(realpath "${1:?usage: publish.sh CHECKOUT OUTPUT}")
output=$(realpath "${2:?usage: publish.sh CHECKOUT OUTPUT}")
project=$(realpath "$(dirname "${BASH_SOURCE[0]}")/..")
if [[ "$checkout" == "$project" || "$checkout" == "$output" ]]; then
  echo "Publishing requires a separate disposable Git checkout" >&2
  exit 1
fi
if [[ "$(git -C "$checkout" rev-parse --show-toplevel)" != "$checkout" ]]; then
  echo "CHECKOUT must be the root of the disposable Git repository" >&2
  exit 1
fi
if [[ -n "$(git -C "$checkout" status --porcelain)" ]]; then
  echo "Publish checkout must be clean" >&2
  exit 1
fi
branches=(json srs shadowrocket)
for branch in "${branches[@]}"; do
  test -f "$output/$branch/manifest.json"
  test -f "$output/$branch/LICENSE.upstream"
done
cmp "$output/json/manifest.json" "$output/srs/manifest.json"
cmp "$output/json/manifest.json" "$output/shadowrocket/manifest.json"

git -C "$checkout" config user.name 'github-actions[bot]'
git -C "$checkout" config user.email '41898282+github-actions[bot]@users.noreply.github.com'
remote_heads=$(git -C "$checkout" ls-remote --heads origin "${branches[@]}")
for branch in "${branches[@]}"; do
  if [[ "$remote_heads" == *"refs/heads/$branch"* ]]; then
    git -C "$checkout" fetch --no-tags --depth=1 origin "refs/heads/$branch"
    git -C "$checkout" checkout -B "$branch" FETCH_HEAD
  else
    # A previous disposable run may still have a local ref for a deleted remote.
    if git -C "$checkout" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$checkout" checkout --detach
      git -C "$checkout" branch -D "$branch"
    fi
    git -C "$checkout" checkout --orphan "$branch"
  fi
  # These are dedicated generated branches; remove obsolete tracked products.
  git -C "$checkout" rm -rf --ignore-unmatch --quiet .
  cp -a "$output/$branch/." "$checkout/"
  git -C "$checkout" add --all
  if ! git -C "$checkout" diff --cached --quiet; then
    git -C "$checkout" commit --quiet -m "Update $branch domain rule-sets"
  fi
done
# If any ref is rejected, no remote branch is advanced. No force push.
git -C "$checkout" push --atomic origin "${branches[@]}"
