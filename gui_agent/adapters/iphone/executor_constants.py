"""Shared execution constants for iPhone Mirroring actions."""

WIN_W = 318
WIN_H = 701
WIN_H_TAP_MAX = WIN_H - 20
SCROLL_TICKS = 3
SCROLL_DELTA = 120
SCROLL_INTERVAL = 0.1

DAEMON_SCROLL_AMOUNT: dict[str, int] = {
    "small":  2,    # ≈1条记录
    "medium": 5,    # ≈1/3屏（标定值）
    "large":  10,   # ≈2/3屏
}
