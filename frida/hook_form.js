/*
 * Frida script to instrument the FORM Swim iOS app.
 * Captures: AES crypto operations, BLE writes/reads, HTTP traffic.
 *
 * Usage:
 *   frida -U -f com.formathletica.form -l hook_form.js --no-pause
 *
 * Then trigger a sync in the FORM app and watch the output.
 */

// ============================================================
// 1. Hook CommonCrypto CCCrypt — captures AES key, IV, plaintext
// ============================================================
var CCCrypt = Module.findExportByName("libcommonCrypto.dylib", "CCCrypt");
if (CCCrypt) {
    Interceptor.attach(CCCrypt, {
        onEnter: function (args) {
            this.op = args[0].toInt32();        // 0=encrypt, 1=decrypt
            this.alg = args[1].toInt32();       // 0=AES128, 1=DES, 2=3DES
            this.opts = args[2].toInt32();       // 1=PKCS7, 2=ECB
            this.key = args[3];
            this.keyLen = args[4].toInt32();
            this.iv = args[5];
            this.dataIn = args[6];
            this.dataInLen = args[7].toInt32();
            this.dataOut = args[8];
            this.dataOutAvail = args[9].toInt32();
            this.dataOutMoved = args[10];
        },
        onLeave: function (retval) {
            if (this.alg !== 0) return; // only AES

            var opName = this.op === 0 ? "ENCRYPT" : "DECRYPT";
            var modeName = (this.opts & 2) ? "ECB" : "CBC";

            var keyBytes = this.key.readByteArray(this.keyLen);
            var ivBytes = (this.iv && !this.iv.isNull() && modeName === "CBC")
                ? this.iv.readByteArray(16) : null;
            var inBytes = this.dataIn.readByteArray(this.dataInLen);
            var outLen = this.dataOutMoved.readU32();
            var outBytes = this.dataOut.readByteArray(outLen);

            console.log("\n====== AES " + opName + " (" + modeName + ", keyLen=" + this.keyLen + ") ======");
            console.log("  Key:        " + hexdump(keyBytes, {length: this.keyLen, header: false, ansi: false}).split("\n").map(function(l){return l.substring(l.indexOf(" ")+1)}).join(""));
            console.log("  Key hex:    " + buf2hex(keyBytes));
            if (ivBytes) {
                console.log("  IV hex:     " + buf2hex(ivBytes));
            }
            console.log("  Input len:  " + this.dataInLen);
            console.log("  Input hex:  " + buf2hex(inBytes));
            console.log("  Output len: " + outLen);
            console.log("  Output hex: " + buf2hex(outBytes));

            // Try to decode output as UTF-8 for readability
            try {
                if (outLen > 0 && outLen < 1024) {
                    var outPtr = this.dataOut;
                    var str = "";
                    var arr = new Uint8Array(outBytes);
                    var printable = true;
                    for (var i = 0; i < arr.length; i++) {
                        if (arr[i] > 0 && arr[i] < 128) {
                            str += String.fromCharCode(arr[i]);
                        } else if (arr[i] === 0) {
                            break;
                        } else {
                            printable = false;
                            break;
                        }
                    }
                    if (printable && str.length > 2) {
                        console.log("  Output str: " + str);
                    }
                }
            } catch(e) {}

            // Also log protobuf decode attempt for small payloads
            if (opName === "DECRYPT" && outLen > 0 && outLen <= 256) {
                console.log("  >>> DECRYPTED DATA — check if this is subscription/entitlement protobuf <<<");
            }
        }
    });
    console.log("[+] Hooked CCCrypt (CommonCrypto AES)");
} else {
    console.log("[-] CCCrypt not found");
}

// Also hook CCCryptorCreate for streaming crypto
var CCCryptorCreate = Module.findExportByName("libcommonCrypto.dylib", "CCCryptorCreate");
if (CCCryptorCreate) {
    Interceptor.attach(CCCryptorCreate, {
        onEnter: function(args) {
            var op = args[0].toInt32();
            var alg = args[1].toInt32();
            var opts = args[2].toInt32();
            var keyLen = args[4].toInt32();
            if (alg === 0) { // AES only
                var keyBytes = args[3].readByteArray(keyLen);
                var iv = args[5];
                var ivBytes = (iv && !iv.isNull()) ? iv.readByteArray(16) : null;
                var opName = op === 0 ? "ENCRYPT" : "DECRYPT";
                var modeName = (opts & 2) ? "ECB" : "CBC";
                console.log("\n====== CCCryptorCreate AES " + opName + " " + modeName + " ======");
                console.log("  Key hex: " + buf2hex(keyBytes));
                if (ivBytes) console.log("  IV hex:  " + buf2hex(ivBytes));
            }
        }
    });
    console.log("[+] Hooked CCCryptorCreate");
}

// ============================================================
// 2. Hook CoreBluetooth BLE writes
// ============================================================
var CBPeripheral = ObjC.classes.CBPeripheral;
if (CBPeripheral) {
    // writeValue:forCharacteristic:type:
    var writeValue = CBPeripheral["- writeValue:forCharacteristic:type:"];
    if (writeValue) {
        Interceptor.attach(writeValue.implementation, {
            onEnter: function(args) {
                var data = new ObjC.Object(args[2]); // NSData
                var char = new ObjC.Object(args[3]);  // CBCharacteristic
                var uuid = char.UUID().UUIDString().toString();
                var bytes = data.bytes();
                var len = data.length();

                if (len > 0) {
                    var raw = bytes.readByteArray(len);
                    console.log("\n------ BLE WRITE to " + uuid + " (" + len + " bytes) ------");
                    console.log("  Hex: " + buf2hex(raw));

                    // Try protobuf decode hint
                    if (len < 500) {
                        var arr = new Uint8Array(raw);
                        // Check if it starts with a protobuf field tag
                        if (arr[0] === 0x08 || arr[0] === 0x0a || arr[0] === 0x10 || arr[0] === 0x12) {
                            console.log("  (looks like protobuf)");
                        }
                    }
                }
            }
        });
        console.log("[+] Hooked CBPeripheral writeValue:forCharacteristic:type:");
    }

    // readValueForCharacteristic:
    var readValue = CBPeripheral["- readValueForCharacteristic:"];
    if (readValue) {
        Interceptor.attach(readValue.implementation, {
            onEnter: function(args) {
                var char = new ObjC.Object(args[2]);
                console.log("[BLE READ] characteristic: " + char.UUID().UUIDString().toString());
            }
        });
        console.log("[+] Hooked CBPeripheral readValueForCharacteristic:");
    }
}

// Hook didUpdateValueForCharacteristic (BLE notifications/reads)
var CBPDelegate = ObjC.classes.CBPeripheralDelegate;
// This is usually implemented in the app's delegate, so we hook the ObjC message dispatch
var resolver = new ApiResolver("objc");
var matches = resolver.enumerateMatches("-[* peripheral:didUpdateValueForCharacteristic:error:]");
matches.forEach(function(match) {
    Interceptor.attach(match.address, {
        onEnter: function(args) {
            var char = new ObjC.Object(args[3]);
            var uuid = char.UUID().UUIDString().toString();
            var val = char.value();
            if (val && !val.isNull()) {
                var data = new ObjC.Object(val);
                var len = data.length();
                if (len > 0) {
                    var raw = data.bytes().readByteArray(len);
                    console.log("\n------ BLE NOTIFY from " + uuid + " (" + len + " bytes) ------");
                    console.log("  Hex: " + buf2hex(raw));
                }
            }
        }
    });
    console.log("[+] Hooked " + match.name);
});

// ============================================================
// 3. Hook NSURLSession for HTTP traffic
// ============================================================
var NSURLSession = ObjC.classes.NSURLSession;
// Hook dataTaskWithRequest:completionHandler:
var resolver2 = new ApiResolver("objc");
var urlMatches = resolver2.enumerateMatches("-[NSURLSession dataTaskWithRequest:completionHandler:]");
urlMatches.forEach(function(match) {
    Interceptor.attach(match.address, {
        onEnter: function(args) {
            var req = new ObjC.Object(args[2]);
            var url = req.URL().absoluteString().toString();
            var method = req.HTTPMethod().toString();
            if (url.indexOf("formathletica") !== -1 || url.indexOf("formswim") !== -1) {
                console.log("\n>>>>>> HTTP " + method + " " + url + " <<<<<<");
                var body = req.HTTPBody();
                if (body && !body.isNull()) {
                    var bodyData = new ObjC.Object(body);
                    var bodyLen = bodyData.length();
                    if (bodyLen > 0 && bodyLen < 2048) {
                        try {
                            var bodyStr = new ObjC.Object(
                                ObjC.classes.NSString.alloc().initWithData_encoding_(body, 4) // NSUTF8
                            ).toString();
                            console.log("  Body: " + bodyStr.substring(0, 500));
                        } catch(e) {
                            console.log("  Body: " + bodyLen + " bytes (binary)");
                        }
                    }
                }
            }
        }
    });
    console.log("[+] Hooked NSURLSession dataTaskWithRequest:");
});

// ============================================================
// Utility
// ============================================================
function buf2hex(buffer) {
    if (!buffer) return "(null)";
    var arr = new Uint8Array(buffer);
    var hex = "";
    for (var i = 0; i < arr.length; i++) {
        hex += ("0" + arr[i].toString(16)).slice(-2);
    }
    return hex;
}

console.log("\n========================================");
console.log("  FORM Swim App Hooks Active");
console.log("  Now open FORM app and trigger a sync");
console.log("========================================\n");
