#!/usr/bin/env bash
# Build a private CLI copy. The supplied source checkout and installed CLI stay intact.
set -euo pipefail

if [[ $# != 2 || "$2" != /* ]]; then
    echo "Usage: bash build.sh <lark-cli repository path or URL> <new absolute executable path>" >&2
    exit 2
fi
helpdesk_cli_source=$1
helpdesk_cli_output=$2
if [[ -e "$helpdesk_cli_output" ]]; then
    echo "Output already exists; choose a new executable path." >&2
    exit 2
fi
helpdesk_cli_patch_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
helpdesk_cli_build_dir=$(mktemp -d "${TMPDIR:-/tmp}/vikingbot-lark-cli.XXXXXXXX")
trap 'rm -rf -- "$helpdesk_cli_build_dir"' EXIT

git clone --quiet --no-checkout -- "$helpdesk_cli_source" "$helpdesk_cli_build_dir/source"
cd -- "$helpdesk_cli_build_dir/source"
git checkout --quiet --detach 4e2cbea94e33913378ce515f0fccfb9b1f2f2bb9
git apply --check "$helpdesk_cli_patch_dir/0001-api-request-headers.patch"
git apply "$helpdesk_cli_patch_dir/0001-api-request-headers.patch"
python3 scripts/fetch_meta.py
go test -race ./cmd/api ./internal/client ./internal/cmdutil ./cmd/auth ./shortcuts/im
go build -trimpath -o "$helpdesk_cli_build_dir/lark-cli-helpdesk" .
mkdir -p -- "$(dirname -- "$helpdesk_cli_output")"
cp -n -- "$helpdesk_cli_build_dir/lark-cli-helpdesk" "$helpdesk_cli_output"
chmod 700 "$helpdesk_cli_output"
echo "Built private Helpdesk CLI: $helpdesk_cli_output"
