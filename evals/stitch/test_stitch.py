"""Unit test: 视觉拼接累积器 (StitchAccumulator + robust_shift, 确定性, 无 LLM).

真机 20260530_130134(账单滚动 3 帧)验证：
  - robust_shift 稳定锁定真实位移(t1->t2≈-369, t2->t3≈-330)，max_shift 放宽不漂移；
  - 失败/无效滚动(shift==0)不追加，不重复采集同一屏；
  - StitchAccumulator 累积到 chunk_px 才吐 chunk，chunk 间保留 overlap；
  - flush 收尾吐出剩余。
"""

import io
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from policy_expr import stitch
from policy_expr.stitch import StitchAccumulator, robust_shift, _gray_u8

SHOTS = Path(__file__).parent / "screenshots"
passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label:50s}{detail}")


def main() -> int:
    print("── stitch accumulator Unit ──")
    names = ["bill_t1.png", "bill_t2.png", "bill_t3.png"]
    if any(not (SHOTS / n).exists() for n in names):
        print(f"  [SKIP] 缺真机截图(gitignored): {names}")
        print("\n0 tests: 0 passed, 0 failed (skipped)")
        return 0
    raw = [(SHOTS / n).read_bytes() for n in names]
    grays = [_gray_u8(b) for b in raw]

    # 1) robust_shift(关键点) 真值 + 质量
    s12, q12 = robust_shift(grays[0], grays[1])
    s23, q23 = robust_shift(grays[1], grays[2])
    ok = (-400 < s12 < -330 and -360 < s23 < -300 and q12 > 0.3 and q23 > 0.3)
    _report("robust_shift 锁定真实位移(t1->t2≈-369,t2->t3≈-330)", ok,
            "" if ok else f"  s12={s12}(q{q12}) s23={s23}(q{q23})")

    # 2) 同帧(零位移)→ shift==0，不追加
    s_same, _ = robust_shift(grays[0], grays[0])
    _report("同帧→shift==0(不追加)", s_same == 0, "" if s_same == 0 else f"  got {s_same}")

    # 2b) 真机回归 20260530_145732：7 条几乎相同的「拼多多」行 + 大片白底，全局像素相关/残差
    #     会被行混叠+白对白骗到 shift=0（成功滚动被误判失败）。关键点法必须测出真实滚动。
    rep = {n: (SHOTS / f"repeat_{n}.png") for n in ("t4", "t4_dragOK", "t4_wheelNoop")}
    if all(p.exists() for p in rep.values()):
        g4 = _gray_u8(rep["t4"].read_bytes())
        s_drag, q_drag = robust_shift(g4, _gray_u8(rep["t4_dragOK"].read_bytes()))
        s_wheel, _ = robust_shift(g4, _gray_u8(rep["t4_wheelNoop"].read_bytes()))
        ok = s_drag < -100 and q_drag > 0.3 and s_wheel == 0
        _report("重复列表+白底：成功drag测出滚动、无效wheel=0", ok,
                "" if ok else f"  drag={s_drag}(q{q_drag}) wheel={s_wheel}")
    else:
        _report("重复列表回归(缺截图,跳过)", True, "  (gitignored)")

    # 3) 累积器：小 chunk_px 强制吐多段，验证 chunk 高度 + overlap
    acc = StitchAccumulator(chunk_px=900, overlap_px=120)
    emitted: list[bytes] = []
    advanced_flags = []
    for b in raw:
        chunks, adv = acc.feed(b)
        emitted.extend(chunks)
        advanced_flags.append(adv)
    tail = acc.flush()
    if tail:
        emitted.append(tail)

    # 三帧都应判为推进(首帧整屏 + 两次成功滚动)
    _report("三帧均 advanced=True(首屏+两次有效滚动)", all(advanced_flags),
            "" if all(advanced_flags) else f"  {advanced_flags}")

    # 至少吐出 1 段；每段(除最后 flush 的尾段)高度==chunk_px
    heights = [Image.open(io.BytesIO(c)).size[1] for c in emitted]
    full = [h for h in heights[:-1]]  # 非尾段
    ok_chunks = len(emitted) >= 1 and all(h == 900 for h in full)
    _report("chunk 高度==chunk_px(尾段除外)", ok_chunks,
            "" if ok_chunks else f"  heights={heights}")

    # 总拼接高度 ≈ 内容带高 + 两次新条带(~369+330) − 各段 overlap 的重复
    # 这里只校验「明显去掉了重叠」：总高 < 三帧内容带朴素相加
    naive = sum(Image.open(io.BytesIO(b)).size[1] for b in raw) * (stitch.CONTENT_BOT - stitch.CONTENT_TOP)
    total_new = sum(full) + (heights[-1] if heights else 0)
    _report("拼接显著短于三帧朴素相加(去掉重叠)", total_new < naive,
            "" if total_new < naive else f"  total={total_new:.0f} naive={naive:.0f}")

    # 4) 真机端到端回归 20260530_153300：「滚动收集账单」成功跑通的 5 帧序列
    #    （含重复的拼多多/零食有鸣行 + 大片白底）。整跑累积器，校验每步都测出滚动、
    #    几何去重后总高显著短于朴素相加。锁住拼接采集链路在真实多帧上的行为。
    run = [SHOTS / f"billrun_t{i}.png" for i in range(1, 6)]
    if all(p.exists() for p in run):
        frames = [p.read_bytes() for p in run]
        rgrays = [_gray_u8(b) for b in frames]
        shifts = [robust_shift(rgrays[i - 1], rgrays[i])[0] for i in range(1, len(rgrays))]
        ok_shifts = all(s < -100 for s in shifts)
        _report("153300：每步滚动都测出(负位移)", ok_shifts,
                "" if ok_shifts else f"  shifts={shifts}")

        racc = StitchAccumulator()
        remitted: list[bytes] = []
        radv = []
        for b in frames:
            ch, a = racc.feed(b)
            remitted.extend(ch)
            radv.append(a)
        rtail = racc.flush()
        if rtail:
            remitted.append(rtail)
        _report("153300：5 帧全 advanced + 吐出多段", all(radv) and len(remitted) >= 2,
                "" if all(radv) and len(remitted) >= 2 else f"  adv={radv} chunks={len(remitted)}")

        rheights = [Image.open(io.BytesIO(c)).size[1] for c in remitted]
        rnaive = sum(Image.open(io.BytesIO(b)).size[1] for b in frames) * (stitch.CONTENT_BOT - stitch.CONTENT_TOP)
        ok_dedup = sum(rheights) < rnaive
        _report("153300：几何去重后总高<朴素相加", ok_dedup,
                "" if ok_dedup else f"  total={sum(rheights)} naive={rnaive:.0f}")
    else:
        _report("153300 端到端回归(缺截图,跳过)", True, "  (gitignored)")

    print(f"\n  [info] 吐出 {len(emitted)} 段, 高度={heights}")
    print(f"{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
