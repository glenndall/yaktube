#!/bin/sh
# Builds yaktube-tv.apk with the Android SDK's own tools (no Gradle).
# Needs: ANDROID_HOME, Java 17, and env KEYSTORE (path to .p12) + KS_PASS.  VERSION (number) is optional.
set -eu
cd "$(dirname "$0")"
BT="$ANDROID_HOME/build-tools/$(ls "$ANDROID_HOME/build-tools" | sort -V | tail -1)"
JAR="$ANDROID_HOME/platforms/$(ls "$ANDROID_HOME/platforms" | grep -E '^android-[0-9]+$' | sort -V | tail -1)/android.jar"
V="${VERSION:-1}"
echo "build-tools: $BT"; echo "platform: $JAR"
rm -rf build && mkdir -p build/classes
cp -R src build/src

"$BT/aapt2" compile --dir res -o build/res.zip
"$BT/aapt2" link -o build/app.apk -I "$JAR" --manifest AndroidManifest.xml build/res.zip \
  --min-sdk-version 22 --target-sdk-version 34 --version-code "$V" --version-name "1.$V"
javac -source 11 -target 11 -nowarn -classpath "$JAR" -d build/classes $(find build/src -name '*.java')
"$BT/d8" --release --min-api 22 --lib "$JAR" --output build $(find build/classes -name '*.class')
(cd build && zip -q app.apk classes.dex)
"$BT/zipalign" -f 4 build/app.apk build/aligned.apk
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-type PKCS12 --ks-pass env:KS_PASS --out yaktube-tv.apk build/aligned.apk
"$BT/apksigner" verify yaktube-tv.apk && ls -l yaktube-tv.apk
