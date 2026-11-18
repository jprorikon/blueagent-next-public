// 登入 token 存**鑰匙圈**（PENDING B.App.4，2026-11-17）。
//
// 以前 C# 用 PlayerPrefs 明文存（`Library/Preferences/<bundle>.plist`，備份裡也看得到）。
// 現在：`kSecClassGenericPassword`，`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`——
// 開機解鎖過一次後背景也讀得到（通知、重連要用），而且**不跟備份搬到別台機器**。
//
// 失敗一律回 0／NULL：C# 那邊（`NextSecureStore`）看到失敗就退回 PlayerPrefs，不能因此登入不了。
// ⚠️ 只用 `extern "C"`，C# 側走 `DllImport("__Internal")`；要連 `Security.framework`
//    （`Editor/IosPlistPostProcess.cs` 的 `LinkFrameworks`）。

#import <Foundation/Foundation.h>
#import <Security/Security.h>

static NSString *const kBaService = @"dev.deepsea.blueagent.token";
static NSString *const kBaAccount = @"login";

static NSMutableDictionary *ba_keychain_query(void)
{
    return [@{
        (__bridge id)kSecClass: (__bridge id)kSecClassGenericPassword,
        (__bridge id)kSecAttrService: kBaService,
        (__bridge id)kSecAttrAccount: kBaAccount,
    } mutableCopy];
}

/// 刪掉 token。回 1＝刪了或本來就沒有。
extern "C" int ba_keychain_delete(void)
{
    OSStatus status = SecItemDelete((__bridge CFDictionaryRef)ba_keychain_query());
    return (status == errSecSuccess || status == errSecItemNotFound) ? 1 : 0;
}

/// 存 token（空字串＝刪）。回 1＝真的進了鑰匙圈。
extern "C" int ba_keychain_set(const char *token)
{
    if (token == NULL || token[0] == '\0') return ba_keychain_delete();
    NSData *data = [[NSString stringWithUTF8String:token] dataUsingEncoding:NSUTF8StringEncoding];
    if (data == nil) return 0;
    ba_keychain_delete();
    NSMutableDictionary *add = ba_keychain_query();
    add[(__bridge id)kSecValueData] = data;
    add[(__bridge id)kSecAttrAccessible] = (__bridge id)kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly;
    OSStatus status = SecItemAdd((__bridge CFDictionaryRef)add, NULL);
    // 失敗要留狀態碼（不印 token）：-34018＝沒有鑰匙圈權限（未簽名／沒有 application-identifier 的包）
    if (status != errSecSuccess) NSLog(@"[BlueAgentKeychain] SecItemAdd failed: %d (fallback to PlayerPrefs)", (int)status);
    return status == errSecSuccess ? 1 : 0;
}

/// 讀 token（沒有＝NULL）。回傳的字串由 C# 的封送層 `free()`，所以一定要 `strdup`。
extern "C" char *ba_keychain_get(void)
{
    NSMutableDictionary *query = ba_keychain_query();
    query[(__bridge id)kSecReturnData] = @YES;
    query[(__bridge id)kSecMatchLimit] = (__bridge id)kSecMatchLimitOne;
    CFTypeRef result = NULL;
    if (SecItemCopyMatching((__bridge CFDictionaryRef)query, &result) != errSecSuccess || result == NULL) return NULL;
    NSData *data = (__bridge_transfer NSData *)result;
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    return text.length > 0 ? strdup(text.UTF8String) : NULL;
}
