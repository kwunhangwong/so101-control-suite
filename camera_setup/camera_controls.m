// UVC controls for the exact selected USB camera.
// Uses device endpoint zero; never seizes the video interface from macOS.
#import <Foundation/Foundation.h>
#import <IOKit/IOCFPlugIn.h>
#import <IOKit/usb/IOUSBLib.h>

static void fail(NSString *message) {
  @throw [NSException exceptionWithName:@"CameraControl" reason:message userInfo:nil];
}

static UInt32 transfer(IOUSBDeviceInterface197 **usb, UInt8 index, UInt8 unit,
                       UInt8 selector, UInt16 size, UInt8 request, UInt32 value) {
  UInt8 bytes[4] = {value & 255, (value >> 8) & 255, (value >> 16) & 255, (value >> 24) & 255};
  IOUSBDevRequestTO command = {.bmRequestType=request == 1 ? 0x21 : 0xa1,
    .bRequest=request, .wValue=selector << 8, .wIndex=(unit << 8) | index,
    .wLength=size, .pData=bytes, .noDataTimeout=1000, .completionTimeout=1000};
  IOReturn result = (*usb)->DeviceRequestTO(usb, &command);
  if (result || command.wLenDone != size)
    fail([NSString stringWithFormat:@"Camera control %u failed (USB 0x%08x). Reconnect the camera and read its settings again.", selector, result]);
  return (UInt32)bytes[0] | ((UInt32)bytes[1] << 8) | ((UInt32)bytes[2] << 16) | ((UInt32)bytes[3] << 24);
}

static NSDictionary *range(IOUSBDeviceInterface197 **usb, UInt8 index, UInt8 unit, UInt8 selector, UInt16 size) {
  NSMutableDictionary *result = [NSMutableDictionary dictionary];
  NSArray *keys = @[@"value", @"min", @"max", @"step", @"default"];
  const UInt8 requests[] = {0x81, 0x82, 0x83, 0x84, 0x87};
  for (int i=0; i<5; i++) result[keys[i]] = @(transfer(usb,index,unit,selector,size,requests[i],0));
  UInt32 info = transfer(usb,index,unit,selector,1,0x86,0);
  if ((info & 3) != 3) fail(@"This camera does not support reading and changing this control.");
  return result;
}

static void validate(NSDictionary *settings, NSString *key, NSDictionary *limits) {
  id value = settings[key];
  if (![value isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID() ||
      !isfinite([value doubleValue]) || [value doubleValue] != [value longLongValue]) fail(@"Camera values must be whole numbers.");
  long long v=[value longLongValue], min=[limits[@"min"] longLongValue], max=[limits[@"max"] longLongValue];
  long long step=MAX(1,[limits[@"step"] longLongValue]);
  if(v<min || v>max || (v-min)%step) fail([NSString stringWithFormat:@"%@ is outside this camera's range or step.",key]);
}

int main(int argc, char **argv) { @autoreleasepool {
  int code=0;
  @try {
    if(argc<3 || argc>4) fail(@"Choose front or wrist and its camera ID, with optional JSON settings.");
    NSString *name=[NSString stringWithUTF8String:argv[1]];
    if(![name isEqual:@"front"] && ![name isEqual:@"wrist"]) fail(@"Unknown camera.");
    NSString *cameraID=[NSString stringWithUTF8String:argv[2]];
    NSRegularExpression *usbID=[NSRegularExpression regularExpressionWithPattern:@"^0x[0-9a-fA-F]{9,16}$" options:0 error:nil];
    if([usbID numberOfMatchesInString:cameraID options:0 range:NSMakeRange(0,cameraID.length)] != 1)
      fail(@"Hardware controls are unavailable for this camera identity. Live capture and ROI selection remain available.");
    unsigned long long identity=0;
    if(![[NSScanner scannerWithString:[cameraID substringFromIndex:2]] scanHexLongLong:&identity]) fail(@"Invalid USB camera ID.");
    NSNumber *locationID=@((UInt32)(identity >> 32));
    NSNumber *vendor=@((UInt16)(identity >> 16)), *product=@((UInt16)identity);
    CFMutableDictionaryRef matching=IOServiceMatching(kIOUSBDeviceClassName);
    CFDictionarySetValue(matching,CFSTR("locationID"),(__bridge CFNumberRef)locationID);
    CFDictionarySetValue(matching,CFSTR("idVendor"),(__bridge CFNumberRef)vendor);
    CFDictionarySetValue(matching,CFSTR("idProduct"),(__bridge CFNumberRef)product);
    io_iterator_t devices=0;
    if(IOServiceGetMatchingServices(kIOMainPortDefault,matching,&devices)) fail(@"Cannot enumerate USB cameras.");
    io_service_t device=IOIteratorNext(devices), duplicate=IOIteratorNext(devices);
    IOObjectRelease(devices);
    if(!device) fail(@"This USB camera is not connected.");
    if(duplicate){IOObjectRelease(device);IOObjectRelease(duplicate);fail(@"The USB camera identity is ambiguous.");}
    IOCFPlugInInterface **plugin=NULL; SInt32 score=0; IOUSBDeviceInterface197 **usb=NULL;
    IOReturn result=IOCreatePlugInInterfaceForService(device,kIOUSBDeviceUserClientTypeID,kIOCFPlugInInterfaceID,&plugin,&score);
    IOObjectRelease(device);
    if(result || !plugin) fail(@"Cannot access this camera's USB controls.");
    (*plugin)->QueryInterface(plugin,CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID197),(void **)&usb);
    (*plugin)->Release(plugin);
    if(!usb) fail(@"Cannot open the camera control endpoint.");
    UInt32 location=0;(*usb)->GetLocationID(usb,&location);
    if(location != locationID.unsignedIntValue) fail(@"USB camera location differs from the selected camera.");
    IOUSBFindInterfaceRequest find={14,1,kIOUSBFindInterfaceDontCare,kIOUSBFindInterfaceDontCare};
    io_iterator_t interfaces=0;
    if((*usb)->CreateInterfaceIterator(usb,&find,&interfaces)) fail(@"No camera control interface.");
    io_service_t service=IOIteratorNext(interfaces);IOObjectRelease(interfaces);
    if(!service) fail(@"No camera control interface.");
    plugin=NULL;
    result=IOCreatePlugInInterfaceForService(service,kIOUSBInterfaceUserClientTypeID,kIOCFPlugInInterfaceID,&plugin,&score);
    IOObjectRelease(service);
    if(result || !plugin) fail(@"Cannot inspect camera control descriptors.");
    IOUSBInterfaceInterface220 **interface=NULL;
    (*plugin)->QueryInterface(plugin,CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID220),(void **)&interface);
    (*plugin)->Release(plugin);
    if(!interface) fail(@"Cannot read camera descriptors.");
    UInt8 index=0,terminal=0,processing=0;(*interface)->GetInterfaceNumber(interface,&index);
    IOUSBDescriptorHeader *descriptor=NULL;
    while((descriptor=(*interface)->FindNextAssociatedDescriptor(interface,descriptor,0x24))){
      UInt8 *b=(UInt8 *)descriptor;
      if(b[0]>=6 && b[2]==2 && b[4]==1 && b[5]==2) terminal=b[3];
      if(b[0]>=4 && b[2]==5) processing=b[3];
    }
    (*interface)->Release(interface);
    if(!terminal || !processing) fail(@"Camera exposure or saturation controls are unavailable.");
    NSDictionary *exposure=range(usb,index,terminal,4,4), *saturation=range(usb,index,processing,7,2);
    NSDictionary *contrast=range(usb,index,processing,3,2);
    UInt32 modes=transfer(usb,index,terminal,2,1,0x84,0);
    UInt32 mode=transfer(usb,index,terminal,2,1,0x81,0);
    if((modes & 9)!=9) fail(@"This camera does not expose the expected auto/manual exposure modes.");
    if(argc==4){
      NSData *data=[[NSString stringWithUTF8String:argv[3]] dataUsingEncoding:NSUTF8StringEncoding];
      NSDictionary *settings=[NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
      if(![settings isKindOfClass:NSDictionary.class] || settings.count!=4 ||
         !settings[@"auto_exposure"] || CFGetTypeID((__bridge CFTypeRef)settings[@"auto_exposure"])!=CFBooleanGetTypeID()) fail(@"Send auto_exposure, exposure, saturation and contrast.");
      validate(settings,@"exposure",exposure);validate(settings,@"saturation",saturation);validate(settings,@"contrast",contrast);
      UInt32 desiredMode=[settings[@"auto_exposure"] boolValue] ? 8 : 1;
      if(mode!=desiredMode) transfer(usb,index,terminal,2,1,1,desiredMode);
      if(transfer(usb,index,terminal,2,1,0x81,0)!=desiredMode) fail(@"Camera did not accept the exposure mode. Read settings again.");
      if(desiredMode==1) transfer(usb,index,terminal,4,4,1,[settings[@"exposure"] unsignedIntValue]);
      transfer(usb,index,processing,7,2,1,[settings[@"saturation"] unsignedIntValue]);
      transfer(usb,index,processing,3,2,1,[settings[@"contrast"] unsignedIntValue]);
      exposure=range(usb,index,terminal,4,4);saturation=range(usb,index,processing,7,2);mode=desiredMode;
      contrast=range(usb,index,processing,3,2);
      if((desiredMode==1 && ![exposure[@"value"] isEqual:settings[@"exposure"]]) || ![saturation[@"value"] isEqual:settings[@"saturation"]] || ![contrast[@"value"] isEqual:settings[@"contrast"]]) fail(@"Camera settings did not match readback. Read settings again before retrying.");
    }
    (*usb)->Release(usb);
    NSDictionary *output=@{@"camera":name, @"camera_unique_id":cameraID,
      @"auto_exposure":mode!=1 ? @YES : @NO, @"exposure":exposure, @"saturation":saturation, @"contrast":contrast};
    NSData *json=[NSJSONSerialization dataWithJSONObject:output options:0 error:nil];
    fwrite(json.bytes,1,json.length,stdout);fputc('\n',stdout);
  } @catch(NSException *exception){
    NSData *json=[NSJSONSerialization dataWithJSONObject:@{@"error":exception.reason ?: @"Camera control failed."} options:0 error:nil];
    fwrite(json.bytes,1,json.length,stdout);fputc('\n',stdout);code=1;
  }
  return code;
}}
