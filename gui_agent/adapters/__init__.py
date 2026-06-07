"""Platform adapters: concrete implementations of gui_agent.core.contracts.

Each subpackage (iphone today; browser / android later) provides the device I/O,
perception, gestures and platform-specific policies that satisfy the neutral
Protocols in gui_agent.core.contracts. Kept import-light: importing this
package must not pull in any heavy platform dependency.
"""
