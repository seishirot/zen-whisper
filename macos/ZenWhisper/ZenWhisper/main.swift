import AppKit

let app = NSApplication.shared
let delegate = MainActor.assumeIsolated {
    AppDelegate()
}
MainActor.assumeIsolated {
    app.delegate = delegate
}
app.run()
