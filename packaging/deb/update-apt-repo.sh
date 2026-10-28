#!/bin/bash
# Add .deb files to the APT repository in SITE (the gh-pages checkout) and
# rebuild its signed indexes. Old versions stay in pool/ so apt can still
# install them.
#
#   GNUPGHOME=… APT_KEY_ID=<fingerprint> packaging/deb/update-apt-repo.sh SITE pkg.deb…
#
# Users add the repository once:
#   curl -fsSL https://digitaldanis.github.io/multidrop/multidrop-archive-keyring.gpg \
#     | sudo tee /usr/share/keyrings/multidrop-archive-keyring.gpg >/dev/null
#   echo "deb [signed-by=/usr/share/keyrings/multidrop-archive-keyring.gpg] \
#     https://digitaldanis.github.io/multidrop stable main" \
#     | sudo tee /etc/apt/sources.list.d/multidrop.list
#   sudo apt update && sudo apt install multidrop

set -euo pipefail
SITE=$(realpath "$1"); shift
KEY=${APT_KEY_ID:?set APT_KEY_ID to the signing key fingerprint}
ARCHES="amd64 arm64 armhf i386"     # the package is Architecture: all

mkdir -p "$SITE/pool/main"
cp "$@" "$SITE/pool/main/"
cd "$SITE"
for arch in $ARCHES; do
  dir=dists/stable/main/binary-$arch
  mkdir -p "$dir"
  apt-ftparchive packages pool > "$dir/Packages"
  gzip -9nkf "$dir/Packages"
done
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin=MultiDrop \
  -o APT::FTPArchive::Release::Label=MultiDrop \
  -o APT::FTPArchive::Release::Suite=stable \
  -o APT::FTPArchive::Release::Codename=stable \
  -o "APT::FTPArchive::Release::Architectures=$ARCHES" \
  -o APT::FTPArchive::Release::Components=main \
  -o APT::FTPArchive::Release::Description="MultiDrop – nearby sharing for Linux" \
  release dists/stable > dists/stable/Release.new
mv dists/stable/Release.new dists/stable/Release
gpg --batch --yes --local-user "$KEY" --armor --detach-sign -o dists/stable/Release.gpg dists/stable/Release
gpg --batch --yes --local-user "$KEY" --clearsign -o dists/stable/InRelease dists/stable/Release
gpg --export "$KEY" > multidrop-archive-keyring.gpg
gpg --export --armor "$KEY" > multidrop-archive-keyring.asc
touch .nojekyll
echo "repository updated: $(ls pool/main | wc -l) package file(s)"
