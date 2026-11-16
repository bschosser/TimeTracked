import AppKit

final class EspressoView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        let cream = NSColor(calibratedRed: 0.98, green: 0.94, blue: 0.84, alpha: 1)
        let coffee = NSColor(calibratedRed: 0.24, green: 0.11, blue: 0.06, alpha: 1)
        let coffeeLight = NSColor(calibratedRed: 0.53, green: 0.28, blue: 0.13, alpha: 1)
        let terracotta = NSColor(calibratedRed: 0.84, green: 0.32, blue: 0.22, alpha: 1)
        let w = bounds.width
        let h = bounds.height
        let cupWidth = min(w * 0.48, 620)
        let cupHeight = cupWidth * 0.62
        let cupX = (w - cupWidth) / 2
        let cupY = h * 0.12
        let saucer = NSRect(x: cupX - cupWidth * 0.18, y: cupY - cupHeight * 0.08, width: cupWidth * 1.36, height: cupHeight * 0.16)
        let bowl = NSRect(x: cupX, y: cupY + cupHeight * 0.14, width: cupWidth, height: cupHeight * 0.68)
        let rim = NSRect(x: cupX - cupWidth * 0.015, y: bowl.maxY - cupHeight * 0.08, width: cupWidth * 1.03, height: cupHeight * 0.19)

        NSColor(calibratedRed: 0.98, green: 0.73, blue: 0.43, alpha: 1).setFill()
        NSBezierPath(ovalIn: saucer).fill()
        NSColor(calibratedRed: 0.99, green: 0.86, blue: 0.62, alpha: 1).setFill()
        NSBezierPath(ovalIn: NSRect(x: saucer.minX + cupWidth * 0.08, y: saucer.minY + cupHeight * 0.035, width: saucer.width - cupWidth * 0.16, height: saucer.height * 0.48)).fill()

        terracotta.setFill()
        NSBezierPath(roundedRect: bowl, xRadius: cupWidth * 0.11, yRadius: cupWidth * 0.11).fill()

        terracotta.setStroke()
        let handle = NSBezierPath()
        handle.lineWidth = cupWidth * 0.055
        handle.appendArc(withCenter: NSPoint(x: bowl.maxX, y: bowl.midY), radius: cupWidth * 0.15, startAngle: -75, endAngle: 75)
        handle.stroke()

        cream.setFill()
        NSBezierPath(ovalIn: rim).fill()
        coffee.setFill()
        NSBezierPath(ovalIn: NSRect(x: rim.minX + cupWidth * 0.07, y: rim.minY + cupHeight * 0.045, width: rim.width - cupWidth * 0.14, height: rim.height * 0.60)).fill()
        coffeeLight.setFill()
        NSBezierPath(ovalIn: NSRect(x: rim.minX + cupWidth * 0.13, y: rim.minY + cupHeight * 0.07, width: rim.width - cupWidth * 0.26, height: rim.height * 0.30)).fill()

        let steam = NSBezierPath()
        steam.lineWidth = max(4, cupWidth * 0.018)
        steam.lineCapStyle = .round
        NSColor(calibratedWhite: 1, alpha: 0.72).setStroke()
        steam.move(to: NSPoint(x: cupX + cupWidth * 0.34, y: rim.maxY + cupHeight * 0.08))
        steam.curve(to: NSPoint(x: cupX + cupWidth * 0.28, y: rim.maxY + cupHeight * 0.48), controlPoint1: NSPoint(x: cupX + cupWidth * 0.48, y: rim.maxY + cupHeight * 0.24), controlPoint2: NSPoint(x: cupX + cupWidth * 0.18, y: rim.maxY + cupHeight * 0.34))
        steam.move(to: NSPoint(x: cupX + cupWidth * 0.53, y: rim.maxY + cupHeight * 0.08))
        steam.curve(to: NSPoint(x: cupX + cupWidth * 0.60, y: rim.maxY + cupHeight * 0.50), controlPoint1: NSPoint(x: cupX + cupWidth * 0.42, y: rim.maxY + cupHeight * 0.26), controlPoint2: NSPoint(x: cupX + cupWidth * 0.72, y: rim.maxY + cupHeight * 0.34))
        steam.move(to: NSPoint(x: cupX + cupWidth * 0.70, y: rim.maxY + cupHeight * 0.08))
        steam.curve(to: NSPoint(x: cupX + cupWidth * 0.76, y: rim.maxY + cupHeight * 0.40), controlPoint1: NSPoint(x: cupX + cupWidth * 0.62, y: rim.maxY + cupHeight * 0.24), controlPoint2: NSPoint(x: cupX + cupWidth * 0.84, y: rim.maxY + cupHeight * 0.28))
        steam.stroke()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var tracker: Process?
    private var trackerExternallyRunning = false
    private var statusTimer: Timer?
    private var pauseWindow: NSWindow?
    #if arch(x86_64)
    private let pythonPath = "/usr/local/bin/python3"
    #else
    private let pythonPath = "/opt/homebrew/bin/python3"
    #endif
    private let port = "8765"

    private var projectRoot: URL {
        Bundle.main.bundleURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        configureStatusItem()
        ensureTracker()
        refreshTrackerState()
        statusTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            self?.refreshTrackerState()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        statusTimer?.invalidate()
    }

    private func configureStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "TT"
        statusItem.button?.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .bold)
        statusItem.button?.toolTip = "Time, Tracked"
        statusItem.menu = makeMenu()
    }

    private func makeMenu() -> NSMenu {
        let menu = NSMenu()
        let stateItem = NSMenuItem(title: "Checking…", action: nil, keyEquivalent: "")
        stateItem.tag = 100
        menu.addItem(stateItem)
        menu.addItem(.separator())

        let toggle = NSMenuItem(title: "Start tracking", action: #selector(toggleTracking), keyEquivalent: "")
        toggle.tag = 101
        toggle.target = self
        menu.addItem(toggle)

        let pause = NSMenuItem(title: "Pause tracking", action: #selector(togglePause), keyEquivalent: "")
        pause.tag = 102
        pause.target = self
        menu.addItem(pause)

        let dashboard = NSMenuItem(title: "Open dashboard", action: #selector(openDashboard), keyEquivalent: "")
        dashboard.target = self
        menu.addItem(dashboard)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Time, Tracked", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        return menu
    }

    private func refreshTrackerState() {
        let running = tracker?.isRunning == true || trackerExternallyRunning
        if let stateItem = statusItem.menu?.item(withTag: 100) {
            stateItem.title = running ? "Tracking is on" : "Tracking is off"
        }
        if let toggle = statusItem.menu?.item(withTag: 101) {
            toggle.title = running ? "Stop tracking" : "Start tracking"
        }
        if let pause = statusItem.menu?.item(withTag: 102) {
            pause.isEnabled = running
            pause.title = pauseState() ? "Resume tracking" : "Pause tracking"
        }
        statusItem.button?.title = running ? "TT" : "TT·"
    }

    @objc private func toggleTracking() {
        if trackerExternallyRunning {
            showError("Tracker already running", "The local tracker was started by another process. Stop that process before using this control.")
            return
        }
        if tracker?.isRunning == true {
            tracker?.terminate()
            tracker = nil
        } else {
            startTracker()
        }
        refreshTrackerState()
    }

    private func startTracker() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: pythonPath)
        process.arguments = [projectRoot.appendingPathComponent("tracker.py").path]
        process.currentDirectoryURL = projectRoot
        process.environment = ProcessInfo.processInfo.environment.merging(["PORT": port]) { _, new in new }
        do {
            try process.run()
            tracker = process
        } catch {
            showError("Could not start tracker", error.localizedDescription)
        }
    }

    private func ensureTracker() {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/status") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                if (response as? HTTPURLResponse)?.statusCode == 200 {
                    self.trackerExternallyRunning = true
                } else {
                    self.startTracker()
                }
                self.refreshTrackerState()
            }
        }.resume()
    }

    @objc private func togglePause() {
        if pauseState() {
            showPauseWindow()
            return
        }
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/pause") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            DispatchQueue.main.async {
                guard let self else { return }
                let paused = self.pauseState()
                self.refreshTrackerState()
                if paused { self.showPauseWindow() } else { self.pauseWindow?.close() }
            }
        }.resume()
    }

    private func showPauseWindow() {
        pauseWindow?.close()

        let visibleFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let windowSize = NSSize(width: visibleFrame.width * 0.95, height: visibleFrame.height * 0.95)
        let content = NSView(frame: NSRect(origin: .zero, size: windowSize))
        content.wantsLayer = true
        content.layer?.backgroundColor = NSColor(calibratedRed: 0.11, green: 0.14, blue: 0.15, alpha: 1).cgColor

        let artwork = EspressoView(frame: NSRect(x: 0, y: windowSize.height * 0.38, width: windowSize.width, height: windowSize.height * 0.54))
        content.addSubview(artwork)

        let title = NSTextField(labelWithString: "Tracking paused")
        title.textColor = .white
        title.font = NSFont.systemFont(ofSize: min(windowSize.width * 0.035, 54), weight: .bold)
        title.alignment = .center
        title.frame = NSRect(x: windowSize.width * 0.08, y: windowSize.height * 0.28, width: windowSize.width * 0.84, height: windowSize.height * 0.08)

        let message = NSTextField(labelWithString: "Was this actually a break, or did work follow you here?")
        message.font = NSFont.systemFont(ofSize: min(windowSize.width * 0.016, 23))
        message.textColor = NSColor(calibratedWhite: 0.78, alpha: 1)
        message.alignment = .center
        message.maximumNumberOfLines = 2
        message.frame = NSRect(x: windowSize.width * 0.12, y: windowSize.height * 0.19, width: windowSize.width * 0.76, height: windowSize.height * 0.08)

        let breakButton = NSButton(title: "Yes, it was a break", target: self, action: #selector(resumeAsBreak))
        let workButton = NSButton(title: "No, count it as work", target: self, action: #selector(resumeAsWork))
        [breakButton, workButton].forEach {
            $0.bezelStyle = .rounded
            $0.font = NSFont.systemFont(ofSize: min(windowSize.width * 0.013, 18), weight: .semibold)
            $0.frame.size.height = windowSize.height * 0.075
        }
        breakButton.keyEquivalent = "\r"
        breakButton.frame.origin = NSPoint(x: windowSize.width * 0.13, y: windowSize.height * 0.07)
        breakButton.frame.size.width = windowSize.width * 0.34
        workButton.frame.origin = NSPoint(x: windowSize.width * 0.53, y: windowSize.height * 0.07)
        workButton.frame.size.width = windowSize.width * 0.34

        [title, message, breakButton, workButton].forEach { content.addSubview($0) }

        let window = NSWindow(contentRect: content.frame, styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Time, Tracked"
        window.contentView = content
        window.backgroundColor = NSColor(calibratedRed: 0.11, green: 0.14, blue: 0.15, alpha: 1)
        window.isReleasedWhenClosed = false
        window.setFrameOrigin(NSPoint(x: visibleFrame.minX + visibleFrame.width * 0.025, y: visibleFrame.minY + visibleFrame.height * 0.025))
        window.level = .floating
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        pauseWindow = window
    }

    @objc private func resumeAsBreak() {
        resumeFromPauseWindow(wasBreak: true)
    }

    @objc private func resumeAsWork() {
        resumeFromPauseWindow(wasBreak: false)
    }

    private func resumeFromPauseWindow(wasBreak: Bool) {
        let window = pauseWindow
        pauseWindow = nil
        window?.orderOut(nil)
        window?.close()
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/resume") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["was_break": wasBreak])
        URLSession.shared.dataTask(with: request) { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.refreshTrackerState() }
        }.resume()
    }

    private func pauseState() -> Bool {
        let file = projectRoot.appendingPathComponent("data/pause.json")
        guard let data = try? Data(contentsOf: file),
              let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
        return value["paused"] as? Bool ?? false
    }

    @objc private func openDashboard() {
        NSWorkspace.shared.open(URL(string: "http://127.0.0.1:\(port)")!)
    }

    @objc private func quit() {
        tracker?.terminate()
        NSApplication.shared.terminate(nil)
    }

    private func showError(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.runModal()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
