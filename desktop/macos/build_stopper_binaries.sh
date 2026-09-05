#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_file="${script_dir}/DecisionEngineStopper.swift"
output_dir="${1:-${script_dir}/bin}"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/decision-engine-stopper.XXXXXX")"
trap 'rm -rf "${build_dir}"' EXIT

mkdir -p "${output_dir}"

build_one() {
  local arch="$1"
  local minimum_target="$2"
  local name="decision-engine-stopper-${arch}"
  local staged="${build_dir}/${name}"

  xcrun swiftc -O -target "${minimum_target}" -o "${staged}" "${source_file}"
  codesign -s - -f "${staged}"
  codesign --verify --strict --verbose=2 "${staged}"
  xcrun lipo "${staged}" -verify_arch "${arch}"
  shasum -a 256 "${staged}"
}

build_one arm64 arm64-apple-macos11
build_one x86_64 x86_64-apple-macos11

install -m 0755 \
  "${build_dir}/decision-engine-stopper-arm64" \
  "${output_dir}/decision-engine-stopper-arm64"
install -m 0755 \
  "${build_dir}/decision-engine-stopper-x86_64" \
  "${output_dir}/decision-engine-stopper-x86_64"

echo "Built signed arm64 and x86_64 Stopper binaries in ${output_dir}"
