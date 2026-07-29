import AppKit

enum StatusIconKind: Equatable {
    case idle
    case inputWaiting
    case recordingSilent
    case recordingSpeech
    case loading
    case processing
    case postprocessing
    case copied
    case warning
}

struct StatusIconFactory {
    static func processingColor(for kind: StatusIconKind) -> NSColor? {
        switch kind {
        case .loading:
            return .systemBlue
        case .processing:
            return .systemOrange
        case .postprocessing:
            return .systemPurple
        case .idle, .inputWaiting, .recordingSilent, .recordingSpeech,
             .copied, .warning:
            return nil
        }
    }

    static func kind(for state: AppState) -> StatusIconKind {
        switch state {
        case .idle:
            return .idle
        case .inputWaiting:
            return .inputWaiting
        case .pasteUnavailable:
            return .warning
        case .recording(_, let voiceActive):
            return voiceActive ? .recordingSpeech : .recordingSilent
        case .preloading:
            return .loading
        case .transcribing, .repairingBackend:
            return .processing
        case .postprocessing:
            return .postprocessing
        case .copied:
            return .copied
        case .copySkipped, .copyFailed, .enhancementWarning, .modelUnavailable,
             .backendRepairRequired, .microphoneError,
             .hotkeyError, .appSignatureChanged, .error:
            return .warning
        }
    }

    static func image(for state: AppState) -> NSImage {
        image(for: state, animationFrame: 0)
    }

    static func image(for state: AppState, animationFrame: Int) -> NSImage {
        image(kind: kind(for: state), animationFrame: animationFrame)
    }

    static func image(kind: StatusIconKind) -> NSImage {
        image(kind: kind, animationFrame: 0)
    }

    static func image(kind: StatusIconKind, animationFrame: Int) -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size)
        image.lockFocus()
        NSColor.clear.setFill()
        NSRect(origin: .zero, size: size).fill()

        switch kind {
        case .idle:
            drawCircle(color: NSColor.systemGray, radius: 4.5, center: CGPoint(x: 9, y: 9))
        case .inputWaiting:
            drawMonogramIcon()
        case .recordingSilent:
            drawCircle(color: NSColor.systemRed, radius: 6.0, center: CGPoint(x: 9, y: 9))
            drawCircle(color: NSColor.white.withAlphaComponent(0.9), radius: 2.0, center: CGPoint(x: 9, y: 9))
        case .recordingSpeech:
            drawCircle(color: NSColor.systemGreen, radius: 6.0, center: CGPoint(x: 9, y: 9))
            drawCircle(color: NSColor.white.withAlphaComponent(0.9), radius: 2.0, center: CGPoint(x: 9, y: 9))
        case .loading, .processing, .postprocessing:
            if let color = processingColor(for: kind) {
                drawProcessingCircle(color: color, animationFrame: animationFrame)
            }
        case .copied:
            drawCircle(color: NSColor.systemGreen, radius: 5.5, center: CGPoint(x: 9, y: 9))
        case .warning:
            NSColor.systemRed.setFill()
            let path = NSBezierPath()
            path.move(to: CGPoint(x: 9, y: 15))
            path.line(to: CGPoint(x: 16, y: 3))
            path.line(to: CGPoint(x: 2, y: 3))
            path.close()
            path.fill()
            NSColor.white.setFill()
            NSRect(x: 8.25, y: 6.5, width: 1.5, height: 5.0).fill()
            drawCircle(color: NSColor.white, radius: 0.9, center: CGPoint(x: 9, y: 4.8))
        }

        image.unlockFocus()
        image.isTemplate = false
        return image
    }

    private static func drawCircle(color: NSColor, radius: CGFloat, center: CGPoint) {
        color.setFill()
        let rect = NSRect(
            x: center.x - radius,
            y: center.y - radius,
            width: radius * 2,
            height: radius * 2
        )
        NSBezierPath(ovalIn: rect).fill()
    }

    private static func drawCircleStroke(color: NSColor, radius: CGFloat, center: CGPoint) {
        color.setStroke()
        let rect = NSRect(
            x: center.x - radius,
            y: center.y - radius,
            width: radius * 2,
            height: radius * 2
        )
        let path = NSBezierPath(ovalIn: rect)
        path.lineWidth = 1.0
        path.stroke()
    }

    private static func drawMonogramIcon() {
        let rect = NSRect(x: 2.8, y: 2.8, width: 12.4, height: 12.4)
        let path = NSBezierPath(roundedRect: rect, xRadius: 3.0, yRadius: 3.0)
        NSColor.labelColor.withAlphaComponent(0.9).setStroke()
        path.lineWidth = 1.25
        path.stroke()

        let text = NSString(string: "Z")
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 10.5, weight: .semibold),
            .foregroundColor: NSColor.labelColor.withAlphaComponent(0.96)
        ]
        let textSize = text.size(withAttributes: attributes)
        text.draw(
            at: CGPoint(x: 9 - textSize.width / 2, y: 8.6 - textSize.height / 2),
            withAttributes: attributes
        )
    }

    private static func drawProcessingCircle(color: NSColor, animationFrame: Int) {
        drawCircle(color: color, radius: 6.0, center: CGPoint(x: 9, y: 9))
        NSColor.white.withAlphaComponent(0.95).setStroke()
        let startAngle = CGFloat((360 - ((animationFrame * 15) % 360)) % 360)
        let path = NSBezierPath()
        path.lineWidth = 1.8
        path.appendArc(
            withCenter: CGPoint(x: 9, y: 9),
            radius: 3.8,
            startAngle: startAngle,
            endAngle: startAngle + 270,
            clockwise: false
        )
        path.stroke()
    }
}
