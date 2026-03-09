# Legal Analysis

## DMCA Interoperability Exception — 17 U.S.C. §1201(f)

This project was developed under the DMCA interoperability exception, which permits reverse engineering of computer programs for the sole purpose of achieving interoperability with independently created programs.

### What we did

1. **BLE protocol analysis**: Connected to our own FORM swim goggles over Bluetooth LE and observed the GATT service/characteristic structure and message formats. BLE is an open standard; reading advertised services requires no circumvention.

2. **Protobuf schema reconstruction**: Decompiled the publicly distributed FORM Android APK (a standard practice for interoperability research) to extract protobuf message definitions. The APK is freely downloadable and not protected by any additional access control beyond standard Android packaging.

3. **API observation**: Used standard HTTPS proxy tools (mitmproxy) on our own device, on our own network, to observe the REST API calls made by the official FORM app. We accessed only our own account and our own data.

### What we did NOT do

- We did not bypass any encryption, DRM, or access controls beyond what is necessary for interoperability
- We did not access any other user's data or accounts
- We did not distribute any FORM proprietary code, binaries, or authentication credentials
- We did not interfere with FORM's servers or services
- We did not reverse engineer firmware or modify the goggles' embedded software

### Legal basis

**17 U.S.C. §1201(f)** states:

> (1) ...a person who has lawfully obtained the right to use a copy of a computer program may circumvent a technological measure that effectively controls access to a particular portion of that program for the sole purpose of identifying and analyzing those elements of the program that are necessary to achieve interoperability of an independently created computer program with other programs...

> (2) ...a person may develop and employ technological means to circumvent a technological measure... for the purpose of enabling interoperability of an independently created computer program with other programs...

> (3) The information acquired through the acts permitted under paragraph (1), and the means permitted under paragraph (2), may be made available to others if the person... provides such information or means solely for the purpose of enabling interoperability of an independently created computer program with other programs...

This project satisfies all three conditions:
- We lawfully purchased and own the FORM goggles
- Our sole purpose is interoperability with third-party training platforms
- We share only the information necessary for interoperability (protocol documentation and schemas)

### Precedent

- **Sega v. Accolade (1992)**: Reverse engineering for interoperability is fair use
- **Sony v. Connectix (2000)**: Intermediate copying during reverse engineering is permissible when the final product is non-infringing
- **Oracle v. Google (2021)**: API interfaces are functional elements subject to fair use

### Disclaimer

This project is not affiliated with, endorsed by, or sponsored by FORM Athletica Inc. FORM is a trademark of FORM Athletica Inc. This legal analysis is provided for informational purposes and does not constitute legal advice.
