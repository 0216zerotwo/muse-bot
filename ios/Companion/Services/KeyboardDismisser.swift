import UIKit
import WebKit

/// A tap anywhere outside a text field puts the keyboard away — in every screen, sheet and full-screen
/// page (they all live in the app's window). Not for a tap in a text field, in a web page (it may be
/// typing into the page), or just above the keyboard where the composer rides (send keeps it up).
@MainActor
final class KeyboardDismisser: NSObject, UIGestureRecognizerDelegate {
    static let shared = KeyboardDismisser()
    private let windows = NSHashTable<UIWindow>.weakObjects()
    private var keyboardTop: CGFloat?

    private override init() {
        super.init()
        NotificationCenter.default.addObserver(self, selector: #selector(keyboardChanged(_:)),
                                               name: UIResponder.keyboardWillChangeFrameNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(keyboardHidden),
                                               name: UIResponder.keyboardWillHideNotification, object: nil)
    }

    /// Watches every window of the app's scenes weakly (once each, releasing destroyed windows automatically).
    func installEverywhere() {
        for scene in UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }) {
            for window in scene.windows where !windows.contains(window) {
                windows.add(window)
                let tap = UITapGestureRecognizer(target: self, action: #selector(tapped(_:)))
                tap.cancelsTouchesInView = false  // the tapped button still gets its tap
                tap.delegate = self
                window.addGestureRecognizer(tap)
            }
        }
    }

    @objc private func tapped(_ recognizer: UITapGestureRecognizer) {
        recognizer.view?.endEditing(true)
    }

    @objc private func keyboardChanged(_ note: Notification) {
        guard let frame = note.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect else { return }
        let screenHeight = UIApplication.shared.connectedScenes
            .compactMap { ($0 as? UIWindowScene)?.screen.bounds.height }
            .max() ?? 10_000
        keyboardTop = frame.minY < screenHeight ? frame.minY : nil
    }

    @objc private func keyboardHidden() { keyboardTop = nil }

    func gestureRecognizer(_ recognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        // The band just above the keyboard is the composer (send, attach): typing goes on.
        if let keyboardTop, touch.location(in: nil).y > keyboardTop - 76 { return false }
        var view = touch.view
        while let current = view {
            if current is UITextField || current is UITextView || current is UISearchBar || current is WKWebView { return false }
            view = current.superview
        }
        return true
    }

    func gestureRecognizer(_ recognizer: UIGestureRecognizer,
                           shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool { true }
}

/// Puts the keyboard away from code (after 前往, 发送 and the like).
@MainActor
func hideKeyboard() {
    UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
}
