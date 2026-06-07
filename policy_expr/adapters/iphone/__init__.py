"""iPhone adapter: screen-mirroring I/O, gestures, perception and pixel recon.

Implements policy_expr.core.contracts against the macOS "iPhone Mirroring" window
(SCK screenshots, zero-preempt / mirroir input backends, picker / gesture
calibration). Import-light by design — submodules pull in Quartz / ONNX / models
on demand, so importing this package itself stays cheap.
"""
