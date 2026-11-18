// iOS 的**選圖**（聊天頁「+」→「写真」，owner 2026-10-29「iOS 也做」）。
//
// 用系統的照片挑選器 `PHPickerViewController`：它一定是「相冊」（不是文件），而且跑在系統的行程裡，
// app **不必**要相簿權限（不跳權限框、不必寫 NSPhotoLibraryUsageDescription）。
//
// 結果的形狀跟 Android 那一支（`PickerActivity` → `Native.emit`）**一模一樣**，C# 側共用
// `NextShell.PollNative`：
//   {"type":"imageSaved","path":"<暫存 jpg>"} ／ {"type":"pickCancelled"}
// 事件放進一個佇列，Unity 每幀 `ba_native_poll()` 拉一筆（主執行緒，不用 UnitySendMessage 找物件名）。
//
// 圖檔一律轉成 JPEG（長邊最多 2048）：HEIC／超大 PNG 伺服器不收（`MediaStore` 只收 png/jpg/webp、6 MB 上限）。
// ⚠️ 只用 `extern "C"`，C# 側走 `DllImport("__Internal")`（靜態連結進 app）。

#import <UIKit/UIKit.h>
#import <PhotosUI/PhotosUI.h>

extern UIViewController *UnityGetGLViewController(void);

static NSMutableArray<NSString *> *ba_native_events;
static id ba_picker_delegate;   // 挑選器的 delegate 是 weak 引用 ⇒ 這裡留一份，選完才放

static void ba_emit(NSDictionary *event)
{
    NSData *json = [NSJSONSerialization dataWithJSONObject:event options:0 error:nil];
    if (json == nil) return;
    NSString *text = [[NSString alloc] initWithData:json encoding:NSUTF8StringEncoding];
    @synchronized ([NSObject class]) {
        if (ba_native_events == nil) ba_native_events = [[NSMutableArray alloc] init];   // alloc：ARC／非 ARC 都留得住
        [ba_native_events addObject:text];
    }
}

/// 長邊縮到 `limit` 以內（不放大）；回 JPEG。
static NSData *ba_jpeg(UIImage *image, CGFloat limit)
{
    CGSize size = image.size;
    CGFloat longest = MAX(size.width, size.height);
    if (longest > limit && longest > 0) {
        CGFloat scale = limit / longest;
        CGSize target = CGSizeMake(floor(size.width * scale), floor(size.height * scale));
        UIGraphicsImageRendererFormat *format = [UIGraphicsImageRendererFormat defaultFormat];
        format.scale = 1.0;
        UIGraphicsImageRenderer *renderer = [[UIGraphicsImageRenderer alloc] initWithSize:target format:format];
        image = [renderer imageWithActions:^(UIGraphicsImageRendererContext *context) {
            [image drawInRect:CGRectMake(0, 0, target.width, target.height)];
        }];
    }
    return UIImageJPEGRepresentation(image, 0.85);
}

@interface BAPickerDelegate : NSObject <PHPickerViewControllerDelegate>
@end

@implementation BAPickerDelegate

- (void)picker:(PHPickerViewController *)picker didFinishPicking:(NSArray<PHPickerResult *> *)results
{
    [picker dismissViewControllerAnimated:YES completion:nil];
    ba_picker_delegate = nil;
    PHPickerResult *first = results.firstObject;
    if (first == nil) {                                   // 按了「取消」
        ba_emit(@{@"type": @"pickCancelled"});
        return;
    }
    NSItemProvider *provider = first.itemProvider;
    if (![provider canLoadObjectOfClass:[UIImage class]]) {
        NSLog(@"[ba-picker] 這個項目讀不成圖片");
        ba_emit(@{@"type": @"pickCancelled", @"why": @"not_image"});
        return;
    }
    [provider loadObjectOfClass:[UIImage class] completionHandler:^(id<NSItemProviderReading> object, NSError *error) {
        UIImage *image = [(NSObject *)object isKindOfClass:[UIImage class]] ? (UIImage *)object : nil;
        NSData *data = image != nil ? ba_jpeg(image, 2048.0) : nil;
        if (data == nil) {
            NSLog(@"[ba-picker] 讀圖失敗：%@", error.localizedDescription);
            ba_emit(@{@"type": @"pickCancelled", @"why": @"load_failed"});
            return;
        }
        NSString *name = [NSString stringWithFormat:@"ba-pick-%@.jpg", [NSUUID UUID].UUIDString];
        NSString *path = [NSTemporaryDirectory() stringByAppendingPathComponent:name];
        if (![data writeToFile:path atomically:YES]) {
            ba_emit(@{@"type": @"pickCancelled", @"why": @"write_failed"});
            return;
        }
        NSLog(@"[ba-picker] 選好了：%lu bytes ⇒ %@", (unsigned long)data.length, path);
        ba_emit(@{@"type": @"imageSaved", @"path": path});
    }];
}

@end

extern "C" void ba_pick_image(void)
{
    dispatch_async(dispatch_get_main_queue(), ^{
        UIViewController *root = UnityGetGLViewController();
        if (root == nil || ba_picker_delegate != nil) {      // 沒有畫面可掛／上一個還開著
            ba_emit(@{@"type": @"pickCancelled", @"why": root == nil ? @"no_view" : @"busy"});
            return;
        }
        PHPickerConfiguration *config = [[PHPickerConfiguration alloc] init];
        config.filter = [PHPickerFilter imagesFilter];
        config.selectionLimit = 1;
        PHPickerViewController *picker = [[PHPickerViewController alloc] initWithConfiguration:config];
        BAPickerDelegate *delegate = [BAPickerDelegate new];
        ba_picker_delegate = delegate;
        picker.delegate = delegate;
        [root presentViewController:picker animated:YES completion:nil];
    });
}

/// 拉一筆事件（沒有＝NULL）。回傳的字串由 C# 的封送層 `free()`，所以一定要 `strdup`。
extern "C" char *ba_native_poll(void)
{
    NSString *next = nil;
    @synchronized ([NSObject class]) {
        if (ba_native_events.count > 0) {
            next = ba_native_events.firstObject;
            [ba_native_events removeObjectAtIndex:0];
        }
    }
    return next != nil ? strdup(next.UTF8String) : NULL;
}
