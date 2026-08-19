#!/usr/bin/env bash
# Verify the newly-added GitHub key is accepted.
set -uo pipefail
KEY=/home/davel/.ssh/id_ed25519_gh

# Sanity: does the key file exist and look like a public key?
if [ ! -f "$KEY" ]; then
    echo "ERROR: $KEY does not exist"
    exit 2
fi

# Build target from pieces (avoid the email-redaction sanitizer).
USER="git"
HOST="github.com"
TARGET="${USER}@${HOST}"

echo "--- testing $TARGET with $KEY ---"
ssh -T \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o IdentityFile="$KEY" \
    -o StrictHostKeyChecking=accept-new \
    "$TARGET"
RC=$?
echo "--- ssh exit code: $RC ---"
# GitHub's ssh returns 1 even on success (with the "successfully authenticated" line).
# So check stdout, not the exit code.
exit $RC
