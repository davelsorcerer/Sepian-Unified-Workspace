#!/usr/bin/env bash
#
# trust-github.sh
#
# Pins github.com's three official SSH host keys (RSA, ECDSA, ed25519)
# into ~/.ssh/known_hosts so future SSH connections to GitHub don't
# prompt to "continue connecting" the first time.
#
# Usage:
#   chmod +x scripts/trust-github.sh
#   ./scripts/trust-github.sh
#
# Re-running this script is safe: it appends (it does not overwrite),
# and ssh-keyscan keys are idempotent enough for our purposes.
#

set -euo pipefail

KNOWN_HOSTS="$HOME/.ssh/known_hosts"

# 1. Ensure ~/.ssh exists with sane permissions.
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

# 2. Ensure known_hosts exists (empty file is fine).
touch "$KNOWN_HOSTS"
chmod 600 "$KNOWN_HOSTS"

# 3. Fetch GitHub's current host keys and append them.
#    ssh-keyscan reads from the official ssh-keyscan(1) command;
#    GitHub publishes these keys at
#    https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
#    We pin all three algorithms (RSA, ECDSA, ed25519) for compatibility.
echo "Fetching github.com host keys via ssh-keyscan..."
ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> "$KNOWN_HOSTS"

echo
echo "✓ Done. GitHub host keys appended to:"
echo "    $KNOWN_HOSTS"
echo
echo "Sanity-check the contents:"
cat "$KNOWN_HOSTS"
echo
echo "Next step: run 'ssh -T [email protected]' to confirm a 'Hi <user>!' greeting."
