#!/bin/sh
# Packs this folder into a .vsix and installs it in VS Code: no vsce, no npm. Run again after an edit.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
version=$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$here/package.json")
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/extension"
cp "$here/package.json" "$here/extension.js" "$tmp/extension/"
cat > "$tmp/extension.vsixmanifest" <<MANIFEST
<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="sentinel-claude" Version="$version" Publisher="sentinel"/>
    <DisplayName>Sentinel: Claude on an alert</DisplayName>
    <Description xml:space="preserve">The Claude button of Sentinel's Watch page.</Description>
    <Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.90.0"/></Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>
MANIFEST
cat > "$tmp/[Content_Types].xml" <<'TYPES'
<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
TYPES
(cd "$tmp" && zip -qr sentinel-claude.vsix extension.vsixmanifest '[Content_Types].xml' extension)
code --install-extension "$tmp/sentinel-claude.vsix" --force
