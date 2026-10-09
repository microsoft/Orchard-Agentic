#!/usr/bin/env bash
# Build a directory holding only the named tasks, as symlinks into the full
# task tree, and point `harbor run --path` at it instead of filtering the tree.
#
# Why: `--include-task-name` makes Harbor test every task directory against
# every pattern, and each test calls `LocalTaskId.get_name()`, which is
# `path.resolve().name`, a fresh round of lstat()s. HARD-51 over the 642-task
# V2 tree comes to ~31,000 resolves, and at ~22 ms each on the /data NFS mount
# that is 11-12 minutes before the first trial starts, on every pass. Over this
# view the same pass costs 51 directory checks and no filtering.
#
# Grading is unchanged: Harbor's Task resolves the symlink on load, so trials
# run on the real directory with the same name and checksum as a full-set run.
# Name the view's leaf directory after the tree's own (`tasks`), because Harbor
# records that basename as the trial's `source` dataset name.
#
# Idempotent and safe to run while another job reads the view: a link that is
# already right is left alone, and only links no longer on the list are
# removed.
#
# Usage:
#   ./scripts/stage_task_subset.sh <tasks_dir> <ids_file> <view_dir>
#
# <ids_file> holds one task directory name per line; `#` comments and blank
# lines are skipped. Every name has to exist under <tasks_dir>.
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $0 <tasks_dir> <ids_file> <view_dir>" >&2
    exit 2
fi
TASKS_DIR="$(cd "$1" && pwd -P)"
IDS_FILE="$2"
VIEW_DIR="$3"

IDS="$(awk '{ sub(/\r$/, ""); sub(/#.*/, ""); gsub(/^[ \t]+|[ \t]+$/, "") } $0 != ""' "${IDS_FILE}")"
if [[ -z "${IDS}" ]]; then
    echo "no task names in ${IDS_FILE}" >&2
    exit 1
fi

MISSING=""
while IFS= read -r id; do
    [[ -d "${TASKS_DIR}/${id}" ]] || MISSING="${MISSING}  ${id}"$'\n'
done <<< "${IDS}"
if [[ -n "${MISSING}" ]]; then
    echo "${IDS_FILE} names tasks that are not in ${TASKS_DIR}:" >&2
    printf '%s' "${MISSING}" >&2
    exit 1
fi

mkdir -p "${VIEW_DIR}"
if [[ "$(cd "${VIEW_DIR}" && pwd -P)" == "${TASKS_DIR}" ]]; then
    echo "<view_dir> is the task tree itself: ${TASKS_DIR}" >&2
    exit 2
fi

while IFS= read -r id; do
    target="${TASKS_DIR}/${id}"
    link="${VIEW_DIR}/${id}"
    if [[ "$(readlink "${link}" 2>/dev/null)" != "${target}" ]]; then
        ln -sfn "${target}" "${link}"
    fi
done <<< "${IDS}"

# Only symlinks are ever removed, so a <view_dir> pointed at a real task tree
# by mistake loses nothing.
for entry in "${VIEW_DIR}"/* ; do
    [[ -L "${entry}" ]] || continue
    grep -Fxq -- "$(basename "${entry}")" <<< "${IDS}" || rm -f -- "${entry}"
done
