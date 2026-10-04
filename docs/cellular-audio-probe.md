# Windows cellular-audio proof, not an autonomous assistant

This experiment uses the resident's existing SIM phone and Windows Bluetooth.
It does not purchase a number, forward calls, answer automatically, identify a
courier, or connect the conversation engine to live calls. Phone Link is a
proprietary hardware-test route, not GuardMate's open AI component or a stable
public automation API. The local Python probe defaults to built-in WinMM. An
explicit optional WDM-KS transport uses Python sounddevice/PortAudio in the
repository venv; neither route installs virtual-audio drivers or changes Windows
audio defaults.

## Observed on October 4, 2026

- The user identified the spare phone as **vivo T2x 5G, Android 15**.
- Phone Link's Calls pairing initially failed and the user reported transient
  Bluetooth connections. After removing just that phone/PC pairing and pairing
  through Calls, the user reported successful pairing.
- Windows subsequently enumerated the phone's hands-free speaker and microphone
  PnP entries. That is device discovery, not a proven digital call-audio path.
- A later read-only WinMM inspection listed only the laptop microphone and
  Realtek speaker. No phone endpoint was available to this probe at that moment.
  The successful later trials used an explicitly selected WDM-KS transport, not
  WinMM or a laptop endpoint.
- The user subsequently reported working human speech in both directions through
  Phone Link. This is a user-reported manual-call pass, not digital backend audio
  proof. PC hangup, locked-phone persistence and both SIMs have not been confirmed.
- A fresh sounddevice inventory exposed paired vivo input/output under
  **Windows WDM-KS**, with one channel and a default rate of 16 kHz. No vivo
  endpoints appeared under WASAPI. Numeric IDs and names are snapshot-specific.
- During an explicitly consenting active test call, a five-second WDM-KS capture
  completed. The first attempt had no caller speech according to the user and
  was not a receive proof. A second capture completed at the user's request;
  after listening, the user confirmed that caller audio was working.
- The local synthetic Piper clip was sent to the vivo output three times, each
  after an explicit user request. Its intended phrase was:
  `GuardMate audio check. Purple parcel forty seven.`
  The user first reported hearing `audio check. purple parcel`. After an
  additional requested playback to check for truncation, the user reported
  `purple parcel 47`, confirming that the identifiable ending reached the caller.
  This is not a complete word-for-word transcript or a formal voice-quality score.
- Receive and transmit now have user-confirmed functional evidence, including
  the transmit phrase's ending. These are separate-direction trials, **not simultaneous duplex**
  or autonomous call handling. The user was instructed to keep the phone off
  speakerphone and mute only the laptop microphone during transmit; those
  physical controls were not independently observed by the agent.
- All five native operations returned successfully with zero model API
  requests. No recording was transcribed or uploaded to an AI service. Native
  reports still say `call_audio_verified: false`; human observations are recorded
  here separately rather than silently changing the machine-reported result.

### Local trial artifacts

Artifacts remain in ignored `.data/cellular-audio/`; do not add recordings to Git.

- `receive-20261004-142232-042098.wav` and its JSON report: five-second initial
  capture; user said the caller was not speaking.
- `receive-20261004-142433-690926.wav` and its JSON report: five-second repeat;
  user-confirmed caller audio.
- `transmit-20261004-142741-387374.json`: first synthetic playback completion.
- `transmit-20261004-142854-066932.json`: explicitly requested repeat playback;
  followed by the user-reported partial phrase match.
- `transmit-20261004-143223-793213.json`: additional explicitly requested playback;
  followed by the user-reported `purple parcel 47` ending.

Before live-agent integration, a separately agreed repeat test should establish
reproducibility with fresh caller-chosen receive and blind transmit phrases, and
that Phone Link remains usable after each probe. The current transmit ending
check passed by user report; full-phrase word-for-word verification remains open.
Simultaneous capture/render, fresh-call recovery, PC hangup,
locked-phone persistence and the actual tested SIM are still unknown. Do not
claim that both Jio and Airtel were tested. Any later Qwen use would send text to
a hosted service and requires separate consent; the current local audio-test
permission does not authorize that upload.

The earlier [WSL preflight](cellular-feasibility.md) remains a separate route.
The installed WSL kernel has `CONFIG_BT_HCIBTUSB_MTK` disabled; USB passthrough,
BlueZ and Asterisk are not configured. No kernel, driver or pairing change was
performed by the agent.

## First gate: human call through Phone Link

Keep the phone nearby and open Phone Link's **Calls** tab. A consenting second
person should call the actual Jio/Airtel number. Answer manually **on the PC**.
Use the laptop's microphone/speakers or wired audio, not Bluetooth earbuds.
Confirm that the PC rings, the resident hears the caller, the caller hears the
resident, and ending the call from the PC works. Document which SIM actually
received the call; do not assume both SIMs have been tested.

No recording is needed at this stage. Bluetooth pairing, seeing a dial pad,
or receiving a notification alone does not pass the audio gate.

## Second gate: explicitly selected digital audio

Run these commands from the repository root using the existing Windows venv.
Inspection opens no audio stream and makes no model API requests:

```powershell
.venv\Scripts\python backend\scripts\probe_cellular_audio.py
.venv\Scripts\python backend\scripts\probe_cellular_audio.py --check-formats
```

The optional format check uses `WAVE_FORMAT_QUERY`, not recording/playback. WinMM
device IDs are directional and can change; its printed names are truncated to
31 UTF-16 characters. Use the exact current printed ID and name. The probe
checks that pair again and checks the opened device's name before queuing audio.
It refuses default devices, the laptop microphone/speaker and missing phone
endpoints. A device list or supported format is still not proof of cellular PCM.

If the phone isn't listed, **stop**. Do not substitute a laptop endpoint to make
the check pass. Inspect again during the consenting test call. If it remains
absent, investigate an alternative API/transport before attempting capture.

### Explicit WDM-KS alternative

On this machine, enumeration found the phone only through WDM-KS. Install the
optional Python packages in this repository's Windows venv, then inspect:

```powershell
.venv\Scripts\python -m pip install -r backend\requirements-cellular.txt
.venv\Scripts\python backend\scripts\probe_cellular_audio.py --transport wdm-ks
```

This inspection does not start a stream. WDM-KS is **not shared WASAPI audio**:
it can lock out Phone Link's use of the endpoint for the active stream, fail if
the pin is already occupied, or temporarily interrupt the human call path.
Do not attempt it on an important call. Live actions additionally require
`--ack-exclusive-audio-risk`. No transport fallback or automatic retry occurs.

Use the newly enumerated WDM-KS ID and exact full name with the record/play
commands below, adding `--transport wdm-ks --ack-exclusive-audio-risk`. These
names may contain line breaks: preserve them exactly when passing arguments
programmatically, rather than retyping or truncating them. Streams use callbacks
because PortAudio's WDM-KS blocking read/write functions are not implemented.
The probe requests mono PCM16 at the explicit 8/16 kHz rate; PortAudio may adapt
native sample format/channel representation internally. Format acceptance alone
does not prove actual phone audio or a conversion-free native transport.

Prepare a short synthetic Piper clip without playing anything:

```powershell
.venv\Scripts\python backend\scripts\probe_cellular_audio.py --prepare-test-clip --wav .data/cellular-audio/local-tx-20261004.wav
```

Existing artifacts are never overwritten. Pick a new filename for another run.
This uses already-installed local speech assets; it does not download anything.
The intended phrase is `GuardMate audio check. Purple parcel forty seven.`

Only on an active, agreed test call, replace `INPUT_ID`, `OUTPUT_ID` and the
quoted names below with values from a fresh inspection. These are deliberately
non-executable placeholders, not the IDs found on this machine:

```powershell
.venv\Scripts\python backend\scripts\probe_cellular_audio.py --record --device-id INPUT_ID --expected-name "EXACT PRINTED PHONE INPUT NAME" --seconds 5 --sample-rate 16000 --wav .data/cellular-audio/receive-test-01.wav --report .data/cellular-audio/receive-test-01.json --ack-consenting-test-call
.venv\Scripts\python backend\scripts\probe_cellular_audio.py --play .data/cellular-audio/local-tx-20261004.wav --device-id OUTPUT_ID --expected-name "EXACT PRINTED PHONE OUTPUT NAME" --report .data/cellular-audio/transmit-test-01.json --ack-consenting-test-call
```

For receive proof, ask the caller to say a fresh fictional phrase and verify it
exists in the selected phone endpoint recording, not merely a local room mic.
For transmit proof, have the caller repeat the generated phrase without telling
them its content beforehand. Mute/disconnect the laptop microphone and keep the
phone off speakerphone during the transmit test, so an acoustic route cannot
masquerade as digital injection. Do not adjust the default Windows devices merely
to make this experimental probe work. Repeat independently before integration.

`audio_operation_completed` means only the native driver returned successfully.
Reports deliberately retain `call_audio_verified: false`: a non-silent recording,
successful write, or generated WAV does not independently verify what the caller
said/heard. Document human observations separately. No automatic retry occurs
after failures or timeouts; partially heard audio is an uncertain outcome.

## Boundaries and retention

- Recordings/playback are at most 10 seconds of PCM16 mono, 8 or 16 kHz.
- Actual audio actions require `--ack-consenting-test-call`, an explicit phone
  endpoint and a bounded subprocess. The child receives an allowlisted environment
  with no model API keys or `.env` loading and is stopped after 20 seconds.
- Complete WAV data stays in child memory until completion. The parent validates
  it and publishes a complete artifact atomically without replacing existing files.
  There is no retained capture after a timed-out native child.
- Files stay locally in ignored `.data/cellular-audio/` until the user deletes
  them. Git ignore is **not encryption or automatic deletion**. Abrupt parent/OS
  termination may leave `.probe-*` temporary files in that directory.
- No automatic transcription, Qwen/Tinker upload, resident-state modification,
  dialing, answering, caller classification or production-model selection occurs.
- Do not expose this experiment over HTTP, record real couriers without consent,
  or use it as an emergency calling service.

Only after both directions pass should we connect fresh per-call sessions,
delivery-mode gating, checked replies and explicit resident takeover. That later
integration still needs an answer/hangup mechanism; audio success alone does not
make GuardMate autonomous.

## Primary references

- [Microsoft Phone Link Calls setup](https://support.microsoft.com/en-us/windows/apps/phonelink/setting-up-calls-in-the-phone-link)
- [Microsoft Calls troubleshooting and Bluetooth-headset limitations](https://support.microsoft.com/en-gb/windows/apps/phonelink/troubleshooting-calls-in-the-phone-link)
- [WinMM recording and query-only open](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveinopen)
- [WinMM playback](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutopen)
- [Waveform buffer lifetime and completion](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/ns-mmeapi-wavehdr)
- [sounddevice raw callback streams](https://python-sounddevice.readthedocs.io/en/0.5.6/api/raw-streams.html)
- [sounddevice explicit device and host-API enumeration](https://python-sounddevice.readthedocs.io/en/0.5.6/api/checking-hardware.html)
- [PortAudio WDM-KS implementation: exclusive access and callback-only I/O](https://github.com/PortAudio/portaudio/blob/master/src/hostapi/wdmks/pa_win_wdmks.c)
