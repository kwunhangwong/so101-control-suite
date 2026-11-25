#import <AVFoundation/AVFoundation.h>
#import <CoreImage/CoreImage.h>
#include <unistd.h>

static void reportError(NSString *message) {
  NSData *data = [NSJSONSerialization dataWithJSONObject:@{@"error":message} options:0 error:nil];
  fwrite(data.bytes, 1, data.length, stdout); fputc('\n', stdout); fflush(stdout);
}

@interface FrameSender : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate>
@property(nonatomic, strong) CIContext *context;
@property(nonatomic) CFAbsoluteTime firstFrame;
@property(nonatomic) CFAbsoluteTime lastFrame;
@property(nonatomic, copy) NSString *cameraID;
@end

@implementation FrameSender
- (void)captureOutput:(AVCaptureOutput *)output didOutputSampleBuffer:(CMSampleBufferRef)sample fromConnection:(AVCaptureConnection *)connection {
  @autoreleasepool {
    CFAbsoluteTime now = CFAbsoluteTimeGetCurrent();
    if (!self.firstFrame) self.firstFrame = now;
    if (now - self.firstFrame < 3.0 || now - self.lastFrame < 0.1) return;
    self.lastFrame = now;
    CVPixelBufferRef pixels = CMSampleBufferGetImageBuffer(sample);
    CIImage *image = [CIImage imageWithCVPixelBuffer:pixels];
    CGColorSpaceRef color = CGColorSpaceCreateDeviceRGB();
    NSData *jpeg = [self.context JPEGRepresentationOfImage:image colorSpace:color options:@{}];
    CGColorSpaceRelease(color);
    if (!jpeg) return;
    NSDictionary *header = @{@"length":@(jpeg.length), @"width":@(CVPixelBufferGetWidth(pixels)), @"height":@(CVPixelBufferGetHeight(pixels)), @"camera_unique_id":self.cameraID};
    NSData *json = [NSJSONSerialization dataWithJSONObject:header options:0 error:nil];
    fwrite(json.bytes, 1, json.length, stdout); fputc('\n', stdout);
    fwrite(jpeg.bytes, 1, jpeg.length, stdout); fflush(stdout);
  }
}
@end

int main(int argc, char **argv) {
  @autoreleasepool {
    if (argc != 2) return 2;
    NSString *requestedID = [NSString stringWithUTF8String:argv[1]];
    if ([requestedID isEqualToString:@"--list"]) {
      NSMutableArray *devices = [NSMutableArray array];
      for (AVCaptureDevice *device in [AVCaptureDevice devicesWithMediaType:AVMediaTypeVideo]) {
        [devices addObject:@{@"id":device.uniqueID, @"name":device.localizedName}];
      }
      NSData *json = [NSJSONSerialization dataWithJSONObject:devices options:0 error:nil];
      fwrite(json.bytes, 1, json.length, stdout); fputc('\n', stdout);
      return 0;
    }
    if (requestedID.length == 0) { reportError(@"No camera is configured. Run configure.py first."); return 4; }
    pid_t parent = getppid();
    dispatch_source_t parentWatch = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_global_queue(QOS_CLASS_UTILITY, 0));
    dispatch_source_set_timer(parentWatch, dispatch_time(DISPATCH_TIME_NOW, NSEC_PER_SEC), NSEC_PER_SEC, NSEC_PER_SEC / 10);
    dispatch_source_set_event_handler(parentWatch, ^{ if (getppid() != parent) exit(0); });
    dispatch_resume(parentWatch);
    if ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo] == AVAuthorizationStatusNotDetermined) {
      puts("{\"event\":\"permission\"}"); fflush(stdout);
      dispatch_semaphore_t permission = dispatch_semaphore_create(0);
      [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo completionHandler:^(BOOL granted) { dispatch_semaphore_signal(permission); }];
      dispatch_semaphore_wait(permission, DISPATCH_TIME_FOREVER);
    }
    if ([AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo] != AVAuthorizationStatusAuthorized) {
      reportError(@"Allow SO101 Cameras in macOS Privacy & Security > Camera, then connect again."); return 3;
    }
    AVCaptureDevice *device = [AVCaptureDevice deviceWithUniqueID:requestedID];
    if (!device) { reportError(@"The selected camera is not connected. Reconnect it or run configure.py to select it again."); return 4; }
    NSError *error = nil;
    AVCaptureDeviceInput *input = [AVCaptureDeviceInput deviceInputWithDevice:device error:&error];
    if (!input) { reportError(error.localizedDescription); return 5; }
    AVCaptureSession *session = [AVCaptureSession new];
    if (![session canAddInput:input]) { reportError(@"Cannot open the camera input."); return 5; }
    [session addInput:input];
    session.sessionPreset = [session canSetSessionPreset:AVCaptureSessionPreset1920x1080] ? AVCaptureSessionPreset1920x1080 : AVCaptureSessionPresetHigh;
    AVCaptureVideoDataOutput *output = [AVCaptureVideoDataOutput new];
    output.alwaysDiscardsLateVideoFrames = YES;
    output.videoSettings = @{(id)kCVPixelBufferPixelFormatTypeKey:@(kCVPixelFormatType_32BGRA)};
    if (![session canAddOutput:output]) { reportError(@"The camera cannot provide video frames."); return 6; }
    [session addOutput:output];
    FrameSender *sender = [FrameSender new]; sender.context = [CIContext context]; sender.cameraID = device.uniqueID;
    [output setSampleBufferDelegate:sender queue:dispatch_queue_create("local.so101.front.frames", DISPATCH_QUEUE_SERIAL)];
    [[NSNotificationCenter defaultCenter] addObserverForName:AVCaptureSessionRuntimeErrorNotification object:session queue:nil usingBlock:^(NSNotification *note) {
      reportError([note.userInfo[AVCaptureSessionErrorKey] localizedDescription]); exit(7);
    }];
    puts("{\"event\":\"warming_up\"}"); fflush(stdout);
    [session startRunning];
    dispatch_main();
  }
}
