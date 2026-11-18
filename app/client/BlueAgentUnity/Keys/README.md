# 本机 Android 调试签名

公开版不包含任何 keystore。`HeadlessBuild.ConfigureAndroidSigning()` 会优先使用你本机提供的 `blueagent-dev.keystore`；文件不存在时使用 Unity 的本机调试签名。

如需固定自己的调试签名，请在本机生成并把密码通过 `BA_KS_PASS` / `BA_KA_PASS` 注入。不要提交密钥、证书指纹或真实开发者账号。

```bash
keytool -genkeypair -v -keystore blueagent-dev.keystore \
  -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000
```
