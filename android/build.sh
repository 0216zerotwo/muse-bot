#!/usr/bin/env bash
# Build the Muse Bot Android app without Gradle: aapt2 -> javac -> d8 -> zipalign -> apksigner.
#
#   bash android/build.sh        (Git Bash on Windows, or macOS / Linux)
#
# Needs a JDK 17 and an Android SDK with build-tools and the android-34+ platform. It looks in
# tools/jdk-17 and tools/android-sdk first, then JAVA_HOME and ANDROID_HOME (set on GitHub's runners).
# Output: android/dist/muse-bot-<version>.apk, signed with android/signing/companion.jks (made on the
# first build; keep it: Android installs an update only when it is signed with the same key).
set -euo pipefail

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) WINDOWS=1 ;;
  *) WINDOWS=0 ;;
esac
if [ "$WINDOWS" -eq 1 ]; then
  # Windows-style paths (E:/...) so both bash and the native tools understand them.
  HERE="$(cd "$(dirname "$0")" && pwd -W)"
  EXE=".exe"; BAT=".bat"; CLASSPATH_SEP=";"
else
  HERE="$(cd "$(dirname "$0")" && pwd)"
  EXE=""; BAT=""; CLASSPATH_SEP=":"
fi
TOOLS="${TOOLS:-$HERE/../tools}"
JDK="$TOOLS/jdk-17"
[ -d "$JDK" ] || JDK="${JAVA_HOME:?no JDK: put JDK 17 in tools/jdk-17 or set JAVA_HOME}"
SDK="$TOOLS/android-sdk"
[ -d "$SDK" ] || SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:?no Android SDK: put it in tools/android-sdk or set ANDROID_HOME}}"

if [ -z "${BUILD_TOOLS:-}" ]; then
  if [ -d "$SDK/build-tools/34.0.0" ]; then
    BUILD_TOOLS="34.0.0"
  elif [ -d "$SDK/build-tools" ]; then
    BUILD_TOOLS="$(ls -1 "$SDK/build-tools" 2>/dev/null | grep -E '^[0-9]+\.' | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1 || true)"
  fi
  BUILD_TOOLS="${BUILD_TOOLS:-34.0.0}"
fi
BT="$SDK/build-tools/$BUILD_TOOLS"

ANDROID_JAR="$SDK/platforms/android-34/android.jar"
if [ ! -f "$ANDROID_JAR" ] && [ -d "$SDK/platforms" ]; then
  for candidate_api in $(ls -1 "$SDK/platforms" 2>/dev/null | sed -n 's/^android-\([0-9][0-9]*\)$/\1/p' | sort -n -r); do
    if [ "$candidate_api" -ge 34 ] && [ -f "$SDK/platforms/android-$candidate_api/android.jar" ]; then
      ANDROID_JAR="$SDK/platforms/android-$candidate_api/android.jar"
      break
    fi
  done
fi

SRC="$HERE/app/src/main"
OUT="$HERE/build"
DIST="$HERE/dist"
SIGNING="$HERE/signing"
MIN_SDK=24
TARGET_SDK=34

export JAVA_HOME="$JDK"
if [ "$WINDOWS" -eq 1 ]; then PATH="$(cygpath -u "$JDK")/bin:$PATH"; else PATH="$JDK/bin:$PATH"; fi
export PATH

for tool in "$BT/aapt2$EXE" "$BT/aapt$EXE" "$BT/d8$BAT" "$BT/zipalign$EXE" "$BT/apksigner$BAT" "$ANDROID_JAR" "$JDK/bin/javac$EXE"; do
  [ -e "$tool" ] || { echo "missing $tool (see android/README.md)" >&2; exit 1; }
done

VERSION="$(sed -n 's/.*android:versionName="\([^"]*\)".*/\1/p' "$SRC/AndroidManifest.xml")"
[ -n "$VERSION" ] || { echo "missing android:versionName in AndroidManifest.xml" >&2; exit 1; }
APK="$DIST/muse-bot-$VERSION.apk"

rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/gen" "$OUT/dex" "$DIST"

echo "== resources"
"$BT/aapt2$EXE" compile --dir "$SRC/res" -o "$OUT/res.zip"
"$BT/aapt2$EXE" link -o "$OUT/unsigned.apk" -I "$ANDROID_JAR" \
  --manifest "$SRC/AndroidManifest.xml" -A "$SRC/assets" --java "$OUT/gen" \
  --min-sdk-version "$MIN_SDK" --target-sdk-version "$TARGET_SDK" "$OUT/res.zip"

echo "== code"
find "$SRC/java" "$OUT/gen" -name '*.java' > "$OUT/sources.txt"
# core-lambda-stubs.jar supplies the lambda bootstrap that android.jar lacks; d8 desugars it.
javac -encoding UTF-8 -source 8 -target 8 -Xlint:-options -J-Duser.language=en \
  -bootclasspath "$ANDROID_JAR$CLASSPATH_SEP$BT/core-lambda-stubs.jar" \
  -d "$OUT/classes" @"$OUT/sources.txt"
jar cf "$OUT/classes.jar" -C "$OUT/classes" .
"$BT/d8$BAT" --release --min-api "$MIN_SDK" --lib "$ANDROID_JAR" --output "$OUT/dex" "$OUT/classes.jar"

echo "== package"
cp "$OUT/unsigned.apk" "$OUT/app.apk"
(cd "$OUT/dex" && "$BT/aapt$EXE" add "$OUT/app.apk" classes.dex >/dev/null)
"$BT/zipalign$EXE" -p -f 4 "$OUT/app.apk" "$OUT/aligned.apk"

echo "== sign"
# One signing key for every build: Android only installs an update signed with the same key.
if [ ! -f "$SIGNING/companion.jks" ]; then
  (
    umask 077
    mkdir -p "$SIGNING"
    chmod 700 "$SIGNING" 2>/dev/null || true
    dd if=/dev/urandom bs=64 count=1 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | cut -c1-32 > "$SIGNING/keystore-password.txt"
    chmod 600 "$SIGNING/keystore-password.txt" 2>/dev/null || true
    keytool -genkeypair -keystore "$SIGNING/companion.jks" -storetype PKCS12 -alias companion \
      -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Muse Bot" \
      -storepass:file "$SIGNING/keystore-password.txt" -keypass:file "$SIGNING/keystore-password.txt"
    chmod 600 "$SIGNING/companion.jks" 2>/dev/null || true
  )
fi
[ -s "$SIGNING/keystore-password.txt" ] || { echo "missing or empty $SIGNING/keystore-password.txt" >&2; exit 1; }
chmod 600 "$SIGNING/keystore-password.txt" "$SIGNING/companion.jks" 2>/dev/null || true
"$BT/apksigner$BAT" sign --ks "$SIGNING/companion.jks" --ks-key-alias companion \
  --ks-pass "file:$SIGNING/keystore-password.txt" --v4-signing-enabled false --out "$APK" "$OUT/aligned.apk"
"$BT/apksigner$BAT" verify "$APK"

echo "== done: $APK"
