#!/usr/bin/env bash
#
# test-github-auth.sh
#
# Verifies that the local SSH setup can authenticate to GitHub.
# Runs ssh -T [email protected] with strict host-key checking and
# parses the three possible outcomes into a clear pass/fail report.
#
# Usage:
#   chmod +x scripts/test-github-auth.sh
#   ./scripts/test-github-auth.sh
#
# Exit codes:
#   0   → Successfully authenticated (key is trusted + recognised)
#   1   → Host key not trusted yet (run trust-github.sh)
#   2   → Key not uploaded / not recognised (key file exists but GitHub doesn't know it)
#   3   → No SSH key found at all (run ssh-keygen)
#   4   → Other SSH/network error
#

set -uo pipefail

GITHUB="[email protected]"
KNOWN_HOSTS="$HOME/.ssh/known_hosts"

# Pick the first usable ssh private key in ~/.ssh.
find_private_key() {
    for candidate in \
        "$HOME/.ssh/id_ed25519" \
        "$HOME/.ssh/id_rsa" \
        "$HOME/.ssh/id_ecdsa" \
        "$HOME/.ssh/id_x25519"
    do
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# Public-key fingerprint (for the report). We resolve via ssh-keygen -lf,
# which works for ed25519 / rsa / ecdsa alike.
fingerprint_of() {
    ssh-keygen -lf "$1" 2>/dev/null | awk '{$1=$2=""; print}' | xargs
}

PUBLIC_KEY="$(find_private_key || true)"
echo "=== GitHub SSH auth check ==="
echo

# ---- Step 1: do we even have a private key? -------------------------------
if [ -z "$PUBLIC_KEY" ]; then
    echo "✗ No SSH private key found in $HOME/.ssh/"
    echo "  Run: ssh-keygen -t ed25519 -C \"[email protected]\""
    exit 3
fi

KEY_FP="$(fingerprint_of "$PUBLIC_KEY")"
echo "✓ Private key:  $PUBLIC_KEY"
echo "  Fingerprint:  $KEY_FP"
echo

# ---- Step 2: do we trust github.com's host key? ---------------------------
if ! grep -qE '^github.com[[:space:]]' "$KNOWN_HOSTS" 2>/dev/null; then
    echo "✗ github.com is not in $KNOWN_HOSTS"
    echo "  Run: ./scripts/trust-github.sh"
    exit 1
fi
echo "✓ github.com host key is trusted"
echo

# ---- Step 3: the actual SSH auth attempt ---------------------------------
echo "→ Running: ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes $GITHUB"
echo "  (BatchMode=yes so ssh never prompts for a passphrase)"
echo

OUT="$(ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$GITHUB" 2>&1)"
RC=$?
echo "$OUT"
echo

# ssh -T exits 1 even on success (because GitHub doesn't provide a shell),
# so we judge by the *message*, not the raw exit code.
if echo "$OUT" | grep -q "successfully authenticated"; then
    USERNAME="$(echo "$OUT" | sed -n 's/Hi \([^!]*\)!.*/\1/p' | xargs)"
    echo "✓ Authenticated as: $USERNAME"
    echo "  (key is both trusted as a host AND recognised by GitHub)"
    exit 0
fi

if echo "$OUT" | grep -q "Permission denied (publickey)"; then
    echo "✗ Host is trusted, but GitHub did not accept this key."
    echo "  Make sure the matching public key is added to your GitHub account:"
    echo "    https://github.com/settings/keys"
    echo "  Public key content:"
    PUB_FILE="${PUBLIC_KEY}.pub"
    if [ -f "$PUB_FILE" ]; then
        cat "$PUB_FILE"
    else
        echo "  (could not locate ${PUB_FILE})"
    fi
    exit 2
fi

if echo "$OUT" | grep -q "Host key verification failed"; then
    echo "✗ Host key verification failed."
    echo "  Re-run: ./scripts/trust-github.sh"
    exit 1
fi

echo "✗ Unexpected SSH error (raw exit code $RC)."
exit 4
