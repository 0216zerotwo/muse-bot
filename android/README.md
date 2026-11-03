# Muse Bot · 安卓 App

一个轻量的安卓 App：第一次打开时填你的 Muse Bot 服务器地址，之后全屏显示服务器上的伙伴页面（`/companion.html`，源码在 `../web`）。聊天、记忆、审批等逻辑都在服务器上，所以服务器更新后 App 不用重装。

网页在 WebView 里做不到的，由 App 自己做，和 iPhone 原生版对齐：

- **＋ 资源库**：拍照、录像、最近的照片和视频（第一次打开时问一次照片权限）、查看全部（系统照片选择器）、添加文件、添加视频
- **提醒到点通知**：App 从服务器读接下来两天要到点的提醒，设成手机自己的通知；打开 App、每次回复结束、后台每隔几小时都会重设，手机重启后也会补上（设置 → 通知 可关）
- **朗读**（手机自带语音）、**语音输入后自动朗读回复**（设置 → 通用）
- **分享给 Muse Bot**：在别的 App 里「分享 → Muse Bot」，链接或文字进到聊天输入框
- **保存、分享、用其他应用打开**：图片和视频存进相册（相册 → Muse Bot），其他文件存到「下载」；PDF、Word 用手机上的应用打开；聊天里的 PDF 卡片显示第一页
- **全屏看视频、应用内打开网页（Chrome 自定义标签页）、复制、轻触震动**

## 拿到安装包

**最简单：GitHub Actions。** 每次推送都会自动打包：仓库的 **Actions → Build Android → 最新一次运行 → Artifacts → muse-bot-android**，下载解压得到 `muse-bot-<版本>.apk`，发到手机上点开安装（允许「安装未知应用」）。

> 安卓只允许用**同一个密钥**签名的新版本覆盖安装。想让每次 CI 的 APK 都能直接覆盖，把你的签名密钥放进仓库的 Secrets：`ANDROID_KEYSTORE_BASE64`（`base64 -w0 android/signing/companion.jks` 的输出）和 `ANDROID_KEYSTORE_PASSWORD`（`android/signing/keystore-password.txt` 的内容）。不设也能用，只是每次都是新密钥，更新前要先卸载旧版。

**自己打包**（不用 Gradle）：需要 JDK 17 和带 build-tools 34、android-34 平台的 Android SDK。放在 `tools/jdk-17`、`tools/android-sdk`，或设置好 `JAVA_HOME`、`ANDROID_HOME`，然后在仓库根目录：

```bash
bash android/build.sh
```

输出 `android/dist/muse-bot-<版本>.apk`。第一次打包会生成签名密钥 `android/signing/`（不进 Git）。**这个目录要备份好、不要删**：以后的新版本必须用它签名，手机才能直接覆盖安装。发新版本前把 `AndroidManifest.xml` 的 `versionCode` 加 1、`versionName` 改成新版本号。

## 装到手机上

**数据线（适合服务器跑在自己电脑上）**：

1. 手机：**设置 → 关于手机**，连点「版本号」7 次打开开发者选项，再在 **开发者选项** 里打开「USB 调试」（小米等机型还要打开「USB 安装」）。
2. 数据线连电脑，手机弹出「允许 USB 调试吗？」时点允许。电脑上保持服务器在运行（`bash tools/run-hermes-gateway.sh`）。
3. 在仓库根目录运行（Windows PowerShell）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File android\install.ps1
   ```

   它会安装 `android/dist` 里最新的 APK，并用 `adb reverse` 把手机的 `127.0.0.1:8766` 转到电脑上的服务器。macOS / Linux 上等价于 `adb install -r android/dist/muse-bot-*.apk && adb reverse tcp:8766 tcp:8766`。
4. 打开手机上的「Muse Bot」，地址填 `http://127.0.0.1:8766`，点「连接」（经数据线转发的连接算作电脑本机，不用配对）。拔掉数据线后重新运行一次脚本。

**服务器在 VPS 上**：把 APK 发到手机上安装，然后在服务器上运行 `sudo npx musebot-server pair`（或 `python3 tools/companion-pair.py https://你的域名`），把它给出的「一键连接」链接粘贴到 App 的地址栏；也可以填地址后输入配对码。配对一次就固定下来，换服务器用 **设置 → 高级 → 更换服务器地址**。

## 这一版的限制

- **只有提醒是通知**：没有推送服务（国内安卓手机大多没有 Google 推送），回复、主动找你、等你确认的消息要打开 App 才看得到。
- **准点**：安卓 12 以上默认不给「精确闹钟」权限，提醒可能晚几分钟；想准点，到 **系统设置 → 应用 → Muse Bot → 闹钟和提醒** 打开。
- 语音输入需要安全连接：`http://127.0.0.1`（数据线）和 `https://` 都可以；局域网 IP 的 `http://` 不行。

## 代码

| 路径 | 说明 |
|---|---|
| `app/src/main/java/.../MainActivity.java` | WebView 外壳：地址设置、权限、下载、分享、朗读、全屏视频、给网页的 `PersonalBotAndroid` 接口 |
| `.../AttachSheet.java`、`MediaLibrary.java`、`FilesProvider.java` | ＋ 资源库面板、照片库与相册、相机和分享用的文件 |
| `.../Reminders.java`、`ReminderReceiver.java`、`BootReceiver.java`、`RefreshJob.java` | 提醒到点通知 |
| `app/src/main/assets/setup.html` | 第一次打开、或连不上时的「连接」页面 |
| `build.sh`、`install.ps1` | 打包、装到手机 |
