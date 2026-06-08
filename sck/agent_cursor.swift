// agent_cursor.swift — 项目自有的 agent 虚拟光标 overlay 守护进程(基础版 + 跨点平滑滑动)
//
// 一个透明、点击穿透、不抢焦点的【小窗口】overlay,每帧跟随到当前(平滑插值的)光标点。
// 蓝渐变箭头 + bloom 光晕(与 cua-driver 同款配色)。从 stdin 读 move 指令平滑滑过去;
// 空闲自动淡出。click/scroll/drag 统一复用它,全局只有这一个 agent 光标;真实物理光标不动。
//
// 为何小窗跟随(而非一个全屏巨窗):macOS "显示器独立空间"下,跨屏巨窗会被整体分配到其中心
// 所在的那块屏(实测跑去副屏)。小窗按其位置被分配到对应那块屏 → 正确落在主屏镜像处。
//
// 协议(stdin,一行一条):
//   move <x> <y>        平滑滑到屏幕点 (x,y) —— 逻辑点、左上原点(同 CGWindow)
//   mode <normal|scroll_up|scroll_down|scroll_left|scroll_right>   切换箭头形状
//   persist <0|1>       常驻:1=关闭空闲自动隐藏(光标停在上次点不消失);0=恢复默认
//   show / hide / quit
//
// 独立进程(不整合进 mirror_daemon):overlay 画在镜像之上会进 SCStream 截图、污染 OCR/YOLO,
// 故保持独立。build: swiftc sck/agent_cursor.swift -o /tmp/agent_cursor

import AppKit
import Foundation

setbuf(stdout, nil)
let DEBUG = ProcessInfo.processInfo.environment["AC_DEBUG"] == "1"
func dbg(_ s: String) { if DEBUG { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) } }

let GLIDE_K: CGFloat = 0.28          // 每帧向目标插值比例(弹性滑动手感)
let ALPHA_K: CGFloat = 0.25
let IDLE_HIDE_S: Double = 4.0
let FPS: Double = 60.0
let BOX: CGFloat = 160               // 小窗边长(容纳箭头 + bloom)

func primaryHeight() -> CGFloat {
    (NSScreen.screens.first { $0.frame.origin == .zero }?.frame.height)
        ?? NSScreen.main?.frame.height ?? 1080
}

// 箭头永远画在小窗中心(窗口移动,内容只画一次)
final class CursorView: NSView {
    var phase: CGFloat = 0   // 呼吸脉冲相位(Controller 每帧推进)
    var mode: String = "normal"  // normal | scroll_up | scroll_down | scroll_left | scroll_right
    override var isFlipped: Bool { false }
    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        if ProcessInfo.processInfo.environment["AC_DEBUG"] == "1" {
            ctx.setFillColor(CGColor(red: 1, green: 0, blue: 1, alpha: 0.6)); ctx.fill(bounds)
        }
        if mode == "normal" { drawPointer(ctx) } else { drawScrollArrow(ctx) }
    }

    private func drawPointer(_ ctx: CGContext) {
        // blue 折纸/飞镖箭头 + 呼吸柔光脉冲 + 落地阴影。nose 在中心(=光标热点)。
        let c = CGPoint(x: bounds.midX, y: bounds.midY)
        let cs = CGColorSpaceCreateDeviceRGB()
        let s: CGFloat = 0.74
        func P(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: c.x + x * s, y: c.y + y * s) }
        let N = P(0, 0); let R = P(34, -16); let M = P(20, -20); let L = P(16, -34)
        let blueHi = CGColor(colorSpace: cs, components: [0.25, 0.60, 1.00, 1.0])!
        let blueLo = CGColor(colorSpace: cs, components: [0.10, 0.35, 0.85, 1.0])!
        let iceHi  = CGColor(colorSpace: cs, components: [0.85, 0.95, 1.00, 1.0])!
        let iceLo  = CGColor(colorSpace: cs, components: [0.60, 0.82, 1.00, 1.0])!
        func outline() -> CGMutablePath {
            let p = CGMutablePath(); p.move(to: N); p.addLine(to: R); p.addLine(to: M); p.addLine(to: L); p.closeSubpath(); return p
        }
        func tri(_ a: CGPoint, _ b: CGPoint, _ d: CGPoint) -> CGMutablePath {
            let p = CGMutablePath(); p.move(to: a); p.addLine(to: b); p.addLine(to: d); p.closeSubpath(); return p
        }
        let pulse = 0.5 + 0.5 * sin(phase)
        let mid = P(16, -18); let glowR = (24 + 16 * pulse) * s; let glowA = 0.14 + 0.26 * pulse
        if let g = CGGradient(colorsSpace: cs, colors: [CGColor(colorSpace: cs, components: [0.25, 0.60, 1.00, glowA])!, CGColor(colorSpace: cs, components: [0.25, 0.60, 1.00, 0.0])!] as CFArray, locations: [0, 1]) {
            ctx.drawRadialGradient(g, startCenter: mid, startRadius: 0, endCenter: mid, endRadius: glowR, options: [])
        }
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: -2.0), blur: 5.0, color: CGColor(colorSpace: cs, components: [0.0, 0.05, 0.20, 0.55])!)
        ctx.addPath(outline()); ctx.setFillColor(blueLo); ctx.fillPath()
        ctx.restoreGState()
        ctx.saveGState(); ctx.addPath(tri(N, R, M)); ctx.clip()
        if let g = CGGradient(colorsSpace: cs, colors: [blueHi, blueLo] as CFArray, locations: [0, 1]) { ctx.drawLinearGradient(g, start: N, end: R, options: []) }
        ctx.restoreGState()
        ctx.saveGState(); ctx.addPath(tri(N, M, L)); ctx.clip()
        if let g = CGGradient(colorsSpace: cs, colors: [iceHi, iceLo] as CFArray, locations: [0, 1]) { ctx.drawLinearGradient(g, start: N, end: L, options: []) }
        ctx.restoreGState()
        ctx.addPath(outline())
        ctx.setStrokeColor(CGColor(colorSpace: cs, components: [0.90, 0.97, 1.00, 0.95])!)
        ctx.setLineWidth(1.0); ctx.setLineJoin(.round); ctx.strokePath()
        let crease = CGMutablePath(); crease.move(to: N); crease.addLine(to: M)
        ctx.addPath(crease)
        ctx.setStrokeColor(CGColor(colorSpace: cs, components: [0.10, 0.35, 0.85, 0.55])!)
        ctx.setLineWidth(0.6); ctx.strokePath()
    }

    private func drawScrollArrow(_ ctx: CGContext) {
        // 方向箭头(scroll 可视化):同款蓝渐变 + bloom,箭头尖指向滚动方向,居中于小窗。
        let c = CGPoint(x: bounds.midX, y: bounds.midY)
        let cs = CGColorSpaceCreateDeviceRGB()
        let s: CGFloat = 1.1
        func P(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: c.x + x * s, y: c.y + y * s) }

        // 尖端/底端因方向而异;isFlipped=false 所以 y+ 为上
        let (tip, bL, notch, bR): (CGPoint, CGPoint, CGPoint, CGPoint)
        switch mode {
        case "scroll_up":
            tip = P(0, 22); bL = P(-20, -10); notch = P(0, 2); bR = P(20, -10)
        case "scroll_down":
            tip = P(0, -22); bL = P(-20, 10); notch = P(0, -2); bR = P(20, 10)
        case "scroll_left":
            tip = P(-22, 0); bL = P(10, -20); notch = P(-2, 0); bR = P(10, 20)
        default: // scroll_right
            tip = P(22, 0); bL = P(-10, -20); notch = P(2, 0); bR = P(-10, 20)
        }

        let arrowPath = CGMutablePath()
        arrowPath.move(to: tip); arrowPath.addLine(to: bR)
        arrowPath.addLine(to: notch); arrowPath.addLine(to: bL); arrowPath.closeSubpath()

        let blueHi = CGColor(colorSpace: cs, components: [0.25, 0.60, 1.00, 1.0])!
        let blueLo = CGColor(colorSpace: cs, components: [0.10, 0.35, 0.85, 1.0])!

        // 1) bloom 光晕(与 pointer 同款)
        let pulse = 0.5 + 0.5 * sin(phase)
        let glowR = (26 + 14 * pulse) * s; let glowA = 0.14 + 0.26 * pulse
        if let g = CGGradient(colorsSpace: cs, colors: [CGColor(colorSpace: cs, components: [0.25, 0.60, 1.00, glowA])!, CGColor(colorSpace: cs, components: [0.25, 0.60, 1.00, 0.0])!] as CFArray, locations: [0, 1]) {
            ctx.drawRadialGradient(g, startCenter: c, startRadius: 0, endCenter: c, endRadius: glowR, options: [])
        }
        // 2) 阴影底色
        ctx.saveGState()
        ctx.setShadow(offset: CGSize(width: 0, height: -2.0), blur: 5.0, color: CGColor(colorSpace: cs, components: [0.0, 0.05, 0.20, 0.55])!)
        ctx.addPath(arrowPath); ctx.setFillColor(blueLo); ctx.fillPath()
        ctx.restoreGState()
        // 3) 渐变(尖→底)
        ctx.saveGState(); ctx.addPath(arrowPath); ctx.clip()
        if let g = CGGradient(colorsSpace: cs, colors: [blueHi, blueLo] as CFArray, locations: [0, 1]) {
            ctx.drawLinearGradient(g, start: tip, end: notch, options: [])
        }
        ctx.restoreGState()
        // 4) 描边
        ctx.addPath(arrowPath)
        ctx.setStrokeColor(CGColor(colorSpace: cs, components: [0.90, 0.97, 1.00, 0.95])!)
        ctx.setLineWidth(1.0); ctx.setLineJoin(.round); ctx.strokePath()
    }
}

final class Controller: @unchecked Sendable {
    let window: NSWindow
    var cur = CGPoint(x: -2000, y: -2000)   // cocoa global
    var target = CGPoint(x: -2000, y: -2000)
    var curAlpha: CGFloat = 0
    var targetAlpha: CGFloat = 0
    var lastMove = CFAbsoluteTimeGetCurrent()
    var haveTarget = false
    var frame = 0
    var persist = false   // 常驻:关掉空闲自动隐藏(browser 用,浮层不进截图)

    init() {
        let w = NSWindow(contentRect: NSRect(x: -2000, y: -2000, width: BOX, height: BOX),
                         styleMask: .borderless, backing: .buffered, defer: false)
        w.isOpaque = false; w.backgroundColor = .clear; w.hasShadow = false
        w.ignoresMouseEvents = true
        w.level = .screenSaver
        w.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        w.alphaValue = 0
        w.contentView = CursorView(frame: NSRect(x: 0, y: 0, width: BOX, height: BOX))
        w.orderFrontRegardless()
        self.window = w
        dbg("[init] BOX=\(BOX) primaryH=\(primaryHeight()) screens=\(NSScreen.screens.map { $0.frame })")
    }

    func toCocoa(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: x, y: primaryHeight() - y) }

    func moveTo(_ x: CGFloat, _ y: CGFloat) {
        let p = toCocoa(x, y)
        if !haveTarget { cur = p; haveTarget = true }
        target = p; targetAlpha = 1; lastMove = CFAbsoluteTimeGetCurrent()
        dbg("[move] cg=(\(Int(x)),\(Int(y))) -> cocoa=(\(Int(p.x)),\(Int(p.y)))")
    }
    func show() { targetAlpha = 1; lastMove = CFAbsoluteTimeGetCurrent() }
    func hide() { targetAlpha = 0 }

    func tick() {
        guard haveTarget else { return }
        cur.x += (target.x - cur.x) * GLIDE_K
        cur.y += (target.y - cur.y) * GLIDE_K
        if !persist && CFAbsoluteTimeGetCurrent() - lastMove > IDLE_HIDE_S { targetAlpha = 0 }
        curAlpha += (targetAlpha - curAlpha) * ALPHA_K
        window.alphaValue = curAlpha
        window.setFrameOrigin(NSPoint(x: cur.x - BOX / 2, y: cur.y - BOX / 2))   // 小窗跟随
        // 推进呼吸脉冲并重绘(仅可见时,省 CPU);周期 ~1.05s
        if curAlpha > 0.02, let v = window.contentView as? CursorView {
            v.phase += 0.10
            v.needsDisplay = true
        }
        frame += 1
        if frame % 30 == 0 {
            dbg("[tick] alpha=\(String(format: "%.2f", curAlpha)) winOrigin=(\(Int(window.frame.minX)),\(Int(window.frame.minY))) screen=\(window.screen.map { "\($0.frame)" } ?? "nil")")
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.finishLaunching()
let ctrl = Controller()

Thread.detachNewThread {
    while let line = readLine(strippingNewline: true) {
        let parts = line.split(separator: " ")
        guard let cmd = parts.first else { continue }
        DispatchQueue.main.async {
            switch cmd {
            case "move":
                if parts.count >= 3, let x = Double(parts[1]), let y = Double(parts[2]) {
                    ctrl.moveTo(CGFloat(x), CGFloat(y))
                }
            case "show": ctrl.show()
            case "hide": ctrl.hide()
            case "mode":
                if parts.count >= 2, let v = ctrl.window.contentView as? CursorView {
                    v.mode = String(parts[1]); v.needsDisplay = true
                }
            case "persist":
                ctrl.persist = parts.count < 2 || parts[1] == "1" || parts[1].lowercased() == "on"
                if ctrl.persist { ctrl.show() }
            case "quit": exit(0)
            default: break
            }
        }
    }
    DispatchQueue.main.async { exit(0) }
}

let timer = Timer(timeInterval: 1.0 / FPS, repeats: true) { _ in ctrl.tick() }
RunLoop.main.add(timer, forMode: .common)
app.run()
