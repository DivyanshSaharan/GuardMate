# Cellular feasibility checkpoint — October 3, 2026

Status: **current WSL/Asterisk route not ready**. This is a read-only hardware/software preflight,
not a completed phone compatibility or two-way-audio test. It does not prove the Android phone
cannot work with a different Linux setup.

Observed on the development machine:

- Windows has an operational MediaTek Bluetooth adapter.
- Ubuntu is installed as WSL2, but currently has no visible USB bus or Bluetooth HCI controller.
- BlueZ, Asterisk, adb and the required USB-passthrough tooling were not installed/found.
- The current WSL kernel configuration disables `CONFIG_BT_HCIBTUSB_MTK`, an additional risk
  for initialization of the available MediaTek adapter.
- No present Android/ADB device was identified by the inventory query. Charging-only mode or
  missing enumeration remains possible; the phone's exact model is still unconfirmed.

No drivers, services, firewall rules, pairing settings, kernels or phone settings were changed.
No phone call or recording occurred. Diagnostics started the existing Ubuntu distro, which was
left running. No AI inference was needed for the preflight.

## Why the cable is not proof

USB charging/data or a working adb connection does not establish a cellular audio bridge.
Android's documented `VOICE_CALL` capture requires a system-only permission; a normal app must
not be assumed capable of capturing both call directions. [Android audio-source documentation](https://developer.android.com/reference/android/media/MediaRecorder.AudioSource#VOICE_CALL)

The planned `chan_mobile` integration requires a functioning Linux Bluetooth subsystem and a
compatible paired phone, with actual audio testing. [Asterisk mobile-channel requirements](https://docs.asterisk.org/Configuration/Channel-Drivers/Mobile-Channel/Mobile-Channel-Requirements/)

WSL USB passthrough requires explicit sharing/attachment. Taking the Bluetooth adapter away from
Windows could disconnect existing peripherals; installing usbipd-win adds a service and firewall
rule. Kernel support/firmware must also be established, not presumed. [Microsoft WSL USB guide](https://learn.microsoft.com/en-us/windows/wsl/connect-usb), [Linux Bluetooth driver configuration](https://github.com/torvalds/linux/blob/master/drivers/bluetooth/Kconfig)

## Next bounded test, only after approval

Use existing hardware and timebox the attempt to two hours. First confirm the exact phone model,
a consenting second phone for calls, and whether a native-Linux boot/another existing Linux machine
is available. No new hardware purchase or paid telephony service is assumed.

October 4 follow-up: a separate [Windows/Phone Link proof path](cellular-audio-probe.md)
uses the existing Windows adapter rather than moving it into WSL. The user reported
successful pairing of a vivo T2x 5G after a targeted re-pair, then working human
speech in both directions through Phone Link. Separate mono 16 kHz WDM-KS trials
then produced a user-confirmed caller recording and a user-reported identifiable
phrase and final number from synthetic transmission. Full-phrase intelligibility,
simultaneous duplex and autonomous call handling remain unverified. No vivo
WASAPI endpoints were found. WDM-KS may conflict with Phone Link's audio ownership.
These Windows observations do not resolve the WSL prerequisites.

The prerequisite is a Linux-visible Bluetooth HCI controller. Driver/kernel changes, installations,
USB attachment and pairing need an explicit setup decision; do not begin an open-ended kernel
rebuild during the challenge. Then require all five observations using a prerecorded reply before
adding AI: incoming call detected, answer succeeds, intelligible caller audio reaches the backend,
the caller hears backend audio, and clean hangup works in both directions.

Discovery/pairing/adb alone is not a pass. Restore normal Bluetooth/phone usability after any test.
If the prerequisite or two-way audio fails within the timebox, finish browser voice and describe
automatic Jio/Airtel call handling as unimplemented. A browser demonstration does not fully solve
the friend's unattended cellular calls.
