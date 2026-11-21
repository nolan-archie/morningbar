import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick

Item {
    id: adapter
    width: 0
    height: 0
    visible: false

    readonly property string desktop: (Quickshell.env("XDG_CURRENT_DESKTOP") || "").toLowerCase()
    readonly property bool isHyprland: desktop.indexOf("hyprland") !== -1
    readonly property bool isNiri: desktop.indexOf("niri") !== -1
    readonly property string backend: isHyprland ? "hyprland" : (isNiri ? "niri" : "generic-wlr")
    readonly property string sessionId: Quickshell.env("XDG_SESSION_ID") || ""
    property bool unlocked: false
    property bool reducedMotion: false
    readonly property bool fullscreen: ToplevelManager.activeToplevel !== null
        && ToplevelManager.activeToplevel.fullscreen

    function refreshLockState() {
        if (isHyprland) {
            if (!hyprLockProbe.running)
                hyprLockProbe.running = true;
            return;
        }
        if (sessionId.length === 0 || logindLockProbe.running)
            return;
        logindLockProbe.running = true;
    }

    function setHyprlandLockState(value) {
        // Hyprland's `locked` query reports the Wayland session-lock state.
        // end-4 uses this protocol without updating logind's LockedHint.
        unlocked = value.trim().toLowerCase() === "false";
    }

    function setLogindLockState(value) {
        if (value.trim().toLowerCase() === "no")
            unlocked = true;
        else
            unlocked = false;
    }

    function readMotionPreference() {
        var override = Quickshell.env("MORNINGBAR_REDUCED_MOTION");
        if (override === "1" || override === "true") {
            reducedMotion = true;
            return;
        }
        if (override === "0" || override === "false") {
            reducedMotion = false;
            return;
        }
        motionProbe.running = true;
    }

    Process {
        id: hyprLockProbe
        command: ["/usr/bin/hyprctl", "locked"]
        stdout: StdioCollector {
            onStreamFinished: adapter.setHyprlandLockState(text)
        }
        stderr: StdioCollector {}
    }

    Process {
        id: logindLockProbe
        command: ["/usr/bin/loginctl", "show-session", adapter.sessionId,
                  "-p", "LockedHint", "--value"]
        stdout: StdioCollector {
            onStreamFinished: adapter.setLogindLockState(text)
        }
        stderr: StdioCollector {}
    }

    Process {
        id: motionProbe
        command: ["/usr/bin/gsettings", "get", "org.gnome.desktop.interface", "enable-animations"]
        stdout: StdioCollector {
            onStreamFinished: {
                if (text.trim().toLowerCase() === "false")
                    adapter.reducedMotion = true;
            }
        }
        stderr: StdioCollector {}
    }

    Timer {
        interval: 1000
        repeat: true
        running: adapter.isHyprland || adapter.sessionId.length > 0
        onTriggered: adapter.refreshLockState()
    }

    Component.onCompleted: {
        refreshLockState();
        readMotionPreference();
    }
}
