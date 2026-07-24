"""Lightweight notification broadcaster used by the scanner.

This module exposes a send_alert(alert) function that will try, in order:
- emit a Socket.IO event to connected browsers (if registered)
- on Windows, attempt to show a native toast and play a short sound (if libraries are available)

The implementations are best-effort and optional so the scanner still runs when dependencies
are missing. The web/browser path is preferred: the client JS registers Notification
permission and plays an audio clip when a new alert arrives.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("scanner.notifications")

_sio = None
_sio_lock = threading.Lock()


def register_socketio(sio):
    """Register a Flask-SocketIO instance so send_alert can emit events."""
    global _sio
    with _sio_lock:
        _sio = sio


def _emit_to_web(alert: dict) -> None:
    try:
        if _sio:
            # emit to all connected clients
            _sio.emit("new_alert", alert, namespace="/")
    except Exception:
        log.exception("emit to web failed")


def _windows_notify(alert: dict) -> None:
    # Try multiple approaches: prefer WinRT (modern toast), fall back to win10toast.
    title = f"{alert.get('ticker','')} — {alert.get('score','')}"
    message = alert.get('headline','')
    sound_path = None
    try:
        from pathlib import Path
        pkg = Path(__file__).resolve().parents[1]
        candidate = pkg / 'static' / 'sounds' / 'cash-register.wav'
        if candidate.exists():
            sound_path = str(candidate)
    except Exception:
        sound_path = None

    # 1) Try WinRT API (requires 'winrt' package)
    try:
        from winrt.windows.ui.notifications import ToastNotificationManager, ToastNotification
        from winrt.windows.data.xml.dom import XmlDocument

        # Build a simple ToastGeneric XML payload
        xml = f"""
<toast>
  <visual>
    <binding template='ToastGeneric'>
      <text>{title}</text>
      <text>{message}</text>
    </binding>
  </visual>
</toast>
"""
        doc = XmlDocument()
        doc.load_xml(xml)
        toast = ToastNotification(doc)
        notifier = ToastNotificationManager.create_toast_notifier()
        notifier.show(toast)
        # Try to play custom sound as a fallback using winsound
        if sound_path:
            try:
                import winsound
                winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass
        return
    except Exception:
        log.debug('winrt toast not available', exc_info=True)

    # 2) Fallback to win10toast
    try:
        from win10toast import ToastNotifier
        notifier = ToastNotifier()
        notifier.show_toast(title, message, threaded=True, icon_path=None, duration=6)
        if sound_path:
            try:
                import winsound
                winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass
        return
    except Exception:
        log.debug('win10toast not available', exc_info=True)

    # Nothing worked; give up quietly
    log.debug("no windows notification method available")


def send_alert(alert: dict) -> None:
    """Broadcast a new alert to available notification channels (best-effort)."""
    # Web emission is immediate and non-blocking for the main thread
    try:
        _emit_to_web(alert)
    except Exception:
        log.exception("web emit failed")

    # Send web push notifications (subscriptions) in background
    try:
        from . import push

        t2 = threading.Thread(target=lambda: push.send_push_to_all(alert), daemon=True)
        t2.start()
    except Exception:
        log.exception("web push dispatch failed")

    # Fire a native Windows toast in a separate thread so the scanner isn't blocked
    try:
        t = threading.Thread(target=_windows_notify, args=(alert,), daemon=True)
        t.start()
    except Exception:
        log.exception("windows notify thread failed")
