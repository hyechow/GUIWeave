"""Android adapter (vision-only): adb screencap perception + adb input control.

Same quadrant as the iphone adapter (pixel perception + mobile interaction); the
only swap is the I/O backend — iphone's mirror daemon / SkyLight zero-preempt path
becomes plain ``adbutils`` (adb ``screencap`` for frames, adb ``input`` for taps /
swipes / keyevents, which inject on-device and are zero-preempt by nature).

Structurally this mirrors ``adapters/browser/`` (the minimal second vision-only
adapter), not the much larger iphone tree: NO SCK, NO PSN tricks, NO recon /
YOLO / OCR, NO HUD. See ``factory.build_android_bundle``.
"""
