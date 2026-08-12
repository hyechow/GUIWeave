# Bundled executable assets

This macOS Developer Preview ships the following immutable assets inside the
plugin. SHA-256 values are recorded so release archives and installed plugin
caches can be verified without executing the files.

| Asset | Architecture | Bytes | SHA-256 |
|---|---:|---:|---|
| `iphone-arm64/sck_server` | arm64 | 116152 | `fd6a6af1463315a3619435a9716203951452829b818b404a04e1553d00f487b8` |
| `iphone-arm64/mirror_daemon` | arm64 | 178560 | `63488d5375980174c8a02be13917a89c4c80f0971d21d9e23a28cdaaad41d043` |
| `android-arm64/scrcpy-macos-aarch64-v4.0/adb` | universal x86_64/arm64 | 19993936 | `9fdf861259dc807937b13afdd5f053c7fda9f3b7726933fe0e0f45130ecb8dc7` |
| `android-arm64/scrcpy-macos-aarch64-v4.0/scrcpy` | arm64 | 8630504 | `38895166923325d6c1f9d1ba782230e0a5743e9ff7e0b13f319174409bd57b0a` |
| `android-arm64/scrcpy-macos-aarch64-v4.0/scrcpy-server` | Android bytecode | 732226 | `84924bd564a1eb6089c872c7521f968058977f91f5ff02514a8c74aff3210f3a` |

The iPhone helpers are GUIWeave preview binaries. They are supported only on
Apple Silicon Macs and rely on macOS system frameworks. Android assets come
from the scrcpy 4.0 macOS aarch64 standalone release; see
`THIRD_PARTY_NOTICES.md` and `licenses/APACHE-2.0.txt`.
