#!/bin/bash

# Build the small set of Pi-only packages into an offline installer payload.
# This deliberately does not use makepkg --syncdeps or pacman -U: build
# dependencies must already exist on the native aarch64 build host, and the
# resulting archives are copied into the payload for a later target-root
# transaction.
set -euo pipefail

readonly SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly RECIPE_DIR="$SCRIPT_DIR/packages"
readonly DEFAULT_METADATA="$SCRIPT_DIR/custom-packages.tsv"
readonly DEFAULT_OUTPUT="$SCRIPT_DIR/../../payload/custom-packages"

die() {
  echo "build-custom-packages: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
Usage:
  build-custom-packages.sh [--output DIRECTORY] [--metadata FILE]
  build-custom-packages.sh --check [--output DIRECTORY] [--metadata FILE]

Build mode requires a native aarch64 host with makepkg and pacman. It never
installs a package into the build host or target. The output directory must be
new or empty and receives package archives, manifest.tsv, and SHA256SUMS.
EOF
  exit 2
}

absolute_path() {
  local path=$1
  if [[ $path == /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s/%s\n' "$PWD" "$path"
  fi
}

regular_file() {
  local path=$1 label=$2
  [[ -f $path && ! -L $path ]] || die "$label is not a regular file: $path"
}

valid_sha256() {
  [[ $1 =~ ^[0-9a-f]{64}$ ]]
}

declare -a PACKAGE_NAMES=()
declare -a PACKAGE_VERSIONS=()
declare -a SOURCE_REVISIONS=()
declare -a SOURCE_SHA256S=()
BUILD_TEMPORARY_ROOT=

cleanup_build_temporary_root() {
  local result=$?
  if [[ -n ${BUILD_TEMPORARY_ROOT:-} ]]; then
    if (( result == 0 )); then
      rm -rf -- "$BUILD_TEMPORARY_ROOT"
    else
      echo "build-custom-packages: failed build files retained at $BUILD_TEMPORARY_ROOT" >&2
    fi
  fi
}

load_metadata() {
  local metadata=$1
  regular_file "$metadata" "metadata"

  PACKAGE_NAMES=()
  PACKAGE_VERSIONS=()
  SOURCE_REVISIONS=()
  SOURCE_SHA256S=()

  local package package_version source_revision source_sha256 extra
  local line_number=0
  while IFS=$'\t' read -r package package_version source_revision source_sha256 extra ||
    [[ -n ${package:-} ]]; do
    ((line_number += 1))
    case ${package:-} in
      ""|\#*) continue ;;
    esac
    [[ -z ${extra:-} ]] || die "metadata line $line_number has too many columns"
    [[ $package =~ ^[A-Za-z0-9@+._:-]+$ ]] || die "invalid package name on metadata line $line_number"
    [[ $package_version =~ ^[A-Za-z0-9@+._:-]+$ ]] || die "invalid package version on metadata line $line_number"
    [[ $source_revision =~ ^[A-Za-z0-9._/-]+$ ]] || die "invalid source revision on metadata line $line_number"
    valid_sha256 "$source_sha256" || die "invalid source SHA-256 on metadata line $line_number"

    local existing
    for existing in "${PACKAGE_NAMES[@]}"; do
      [[ $existing != "$package" ]] || die "duplicate package in metadata: $package"
    done
    PACKAGE_NAMES+=("$package")
    PACKAGE_VERSIONS+=("$package_version")
    SOURCE_REVISIONS+=("$source_revision")
    SOURCE_SHA256S+=("$source_sha256")
  done < "$metadata"

  ((${#PACKAGE_NAMES[@]} > 0)) || die "metadata has no packages: $metadata"
}

check_bundle() {
  local output=$1 metadata=$2
  load_metadata "$metadata"
  [[ -d $output && ! -L $output ]] || die "bundle output is not a real directory: $output"

  local manifest="$output/manifest.tsv"
  local sums="$output/SHA256SUMS"
  local json_manifest="$output/manifest.json"
  regular_file "$manifest" "bundle manifest"
  regular_file "$sums" "bundle checksums"
  regular_file "$json_manifest" "JSON bundle manifest"

  local -a seen_packages=()
  local -a seen_files=()
  local package version architecture source_revision source_sha256 package_sha256 package_signature filename extra
  local line_number=0
  while IFS=$'\t' read -r package version architecture source_revision source_sha256 package_sha256 package_signature filename extra ||
    [[ -n ${package:-} ]]; do
    ((line_number += 1))
    case ${package:-} in
      ""|\#*) continue ;;
    esac
    [[ -z ${extra:-} ]] || die "manifest line $line_number has too many columns"
    [[ $package =~ ^[A-Za-z0-9@+._:-]+$ ]] || die "invalid package name in manifest"
    [[ $version =~ ^[A-Za-z0-9@+._:-]+$ ]] || die "invalid package version in manifest"
    [[ $architecture == aarch64 ]] || die "custom package is not aarch64: $package"
    [[ $source_revision =~ ^[A-Za-z0-9._/-]+$ ]] || die "invalid source revision in manifest"
    valid_sha256 "$source_sha256" || die "invalid source SHA-256 in manifest"
    valid_sha256 "$package_sha256" || die "invalid package SHA-256 in manifest"
    [[ $package_signature == unsigned ]] || die "unsupported package signature state: $package"
    [[ -n $filename && $filename == "$(basename -- "$filename")" ]] || die "invalid archive filename in manifest"

    local index=-1 i
    for i in "${!PACKAGE_NAMES[@]}"; do
      if [[ ${PACKAGE_NAMES[$i]} == "$package" ]]; then
        index=$i
        break
      fi
    done
    ((index >= 0)) || die "manifest contains an unlisted package: $package"
    [[ ${PACKAGE_VERSIONS[$index]} == "$version" ]] || die "manifest version differs from metadata: $package"
    [[ ${SOURCE_REVISIONS[$index]} == "$source_revision" ]] || die "manifest source revision differs from metadata: $package"
    [[ ${SOURCE_SHA256S[$index]} == "$source_sha256" ]] || die "manifest source SHA-256 differs from metadata: $package"

    for i in "${!seen_packages[@]}"; do
      [[ ${seen_packages[$i]} != "$package" ]] || die "duplicate package in manifest: $package"
      [[ ${seen_files[$i]} != "$filename" ]] || die "duplicate archive in manifest: $filename"
    done
    local archive="$output/$filename"
    regular_file "$archive" "package archive"
    local actual_sha256
    actual_sha256=$(sha256sum -- "$archive" | awk '{print $1}')
    [[ $actual_sha256 == "$package_sha256" ]] || die "package checksum mismatch: $filename"
    seen_packages+=("$package")
    seen_files+=("$filename")
  done < "$manifest"

  ((${#seen_packages[@]} == ${#PACKAGE_NAMES[@]})) || die "manifest package count does not match metadata"
  (cd -- "$output" && sha256sum --check --strict SHA256SUMS >/dev/null) || die "SHA256SUMS verification failed"

  local entry name known
  shopt -s nullglob
  for entry in "$output"/*; do
    [[ -f $entry && ! -L $entry ]] || die "bundle contains a non-file entry: $entry"
    name=$(basename -- "$entry")
    case $name in
      manifest.tsv|manifest.json|SHA256SUMS) continue ;;
    esac
    known=0
    for filename in "${seen_files[@]}"; do
      [[ $filename == "$name" ]] || continue
      known=1
      break
    done
    ((known == 1)) || die "bundle contains an unlisted file: $name"
  done
  shopt -u nullglob
  echo "Bundle valid: $output (${#seen_packages[@]} packages)"
}

build_bundle() {
  local output=$1 metadata=$2
  load_metadata "$metadata"
  [[ $(uname -m) == aarch64 ]] || die "build must run on a native aarch64 host"
  command -v makepkg >/dev/null 2>&1 || die "makepkg is required"
  command -v bsdtar >/dev/null 2>&1 || die "bsdtar is required for package metadata"
  command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"

  if [[ -L $output || -e $output && ! -d $output ]]; then
    die "output is not a directory: $output"
  fi
  if [[ -d $output ]]; then
    local existing
    existing=$(find "$output" -mindepth 1 -maxdepth 1 -print -quit)
    [[ -z $existing ]] || die "output directory must be empty: $output"
  else
    mkdir -p -- "$output"
  fi

  local temporary_root
  BUILD_TEMPORARY_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/omarchy-pi-custom-packages.XXXXXXXX")
  trap cleanup_build_temporary_root EXIT
  local stage="$BUILD_TEMPORARY_ROOT/stage"
  mkdir -- "$stage"

  local -a manifest_rows=()
  local -a archives=()
  local index package recipe_dir build_dir archive_count archive archive_name package_info
  local actual_package actual_version actual_arch package_sha256
  for index in "${!PACKAGE_NAMES[@]}"; do
    package=${PACKAGE_NAMES[$index]}
    recipe_dir="$RECIPE_DIR/$package"
    regular_file "$recipe_dir/PKGBUILD" "PKGBUILD for $package"
    build_dir=$(mktemp -d "$BUILD_TEMPORARY_ROOT/$package.XXXXXXXX")
    cp -a -- "$recipe_dir/." "$build_dir/"

    echo "Building $package ${PACKAGE_VERSIONS[$index]}..."
    (
      cd -- "$build_dir"
      # No --syncdeps and no --install: this command only builds an archive.
      # --nodeps permits a private extracted fakeroot helper. Missing
      # compilers or libraries still fail in the package build itself.
      PKGDEST="$build_dir" SRCDEST="$build_dir/src-cache" \
        SRCPKGDEST="$build_dir/srcpkg" LOGDEST="$build_dir/log" \
        BUILDDIR="$build_dir/work" \
        makepkg --nodeps --noconfirm --clean --cleanbuild
    )

    archives=()
    mapfile -t archives < <(find "$build_dir" -mindepth 1 -maxdepth 1 -type f -name '*.pkg.tar.*' -print)
    archive_count=${#archives[@]}
    ((archive_count == 1)) || die "$package build produced $archive_count package archives"
    archive=${archives[0]}
    archive_name=$(basename -- "$archive")
    [[ $archive_name == "$(basename -- "$archive_name")" ]] || die "invalid package archive name: $archive_name"

    package_info=$(bsdtar -xOf "$archive" .PKGINFO) ||
      die "bsdtar could not read package metadata: $archive_name"
    actual_package=$(awk -F ' = ' '$1 == "pkgname" { print $2; exit }' <<< "$package_info")
    actual_version=$(awk -F ' = ' '$1 == "pkgver" { print $2; exit }' <<< "$package_info")
    actual_arch=$(awk -F ' = ' '$1 == "arch" { print $2; exit }' <<< "$package_info")
    [[ $actual_package == "$package" ]] || die "archive package name is $actual_package, expected $package"
    [[ $actual_version == "${PACKAGE_VERSIONS[$index]}" ]] || die "archive version is $actual_version, expected ${PACKAGE_VERSIONS[$index]}"
    [[ $actual_arch == aarch64 ]] || die "archive architecture is $actual_arch, expected aarch64"

    package_sha256=$(sha256sum -- "$archive" | awk '{print $1}')
    install -m 644 -- "$archive" "$stage/$archive_name"
    manifest_rows+=("$actual_package"$'\t'"$actual_version"$'\t'"$actual_arch"$'\t'"${SOURCE_REVISIONS[$index]}"$'\t'"${SOURCE_SHA256S[$index]}"$'\t'"$package_sha256"$'\tunsigned'$'\t'"$archive_name")
  done

  {
    printf '%s\n' '# schema_version=1'
    printf '%s\n' '# columns=package version architecture source_revision source_sha256 package_sha256 package_signature filename'
    printf '%s\n' "${manifest_rows[@]}"
  } > "$stage/manifest.tsv"
  {
    local row filename digest
    for row in "${manifest_rows[@]}"; do
      IFS=$'\t' read -r _ _ _ _ _ digest _ filename <<< "$row"
      printf '%s  %s\n' "$digest" "$filename"
    done
  } > "$stage/SHA256SUMS"
  {
    local row name version architecture revision source_sha digest signature filename separator
    printf '%s\n' '{' '  "schema_version": 1,' '  "package_signatures": "unsigned",' '  "archives": ['
    separator=''
    for row in "${manifest_rows[@]}"; do
      IFS=$'\t' read -r name version architecture revision source_sha digest signature filename <<< "$row"
      printf '%s    {"name":"%s","package":"%s","version":"%s","architecture":"%s","source_revision":"%s","source_sha256":"%s","sha256":"%s","signature":"%s"}' "$separator" "$filename" "$name" "$version" "$architecture" "$revision" "$source_sha" "$digest" "$signature"
      separator=$',\n'
    done
    printf '%s\n' '  ]' '}'
  } > "$stage/manifest.json"
  chmod 644 "$stage/manifest.tsv" "$stage/manifest.json" "$stage/SHA256SUMS"

  cp -a -- "$stage/." "$output/"
  trap - EXIT
  cleanup_build_temporary_root
  BUILD_TEMPORARY_ROOT=
  check_bundle "$output" "$metadata"
  echo "Custom package payload ready: $output"
}

mode=build
output=$(absolute_path "$DEFAULT_OUTPUT")
metadata=$DEFAULT_METADATA
while (($#)); do
  case $1 in
    --check)
      mode=check
      shift
      ;;
    --output)
      (($# >= 2)) || usage
      output=$(absolute_path "$2")
      shift 2
      ;;
    --metadata)
      (($# >= 2)) || usage
      metadata=$(absolute_path "$2")
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

case $mode in
  build) build_bundle "$output" "$metadata" ;;
  check) check_bundle "$output" "$metadata" ;;
  *) die "unknown mode: $mode" ;;
esac
