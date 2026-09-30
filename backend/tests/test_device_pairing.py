from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app.integrations import device
from app.integrations.errors import IntegrationError


def test_pairing_route_passes_secret_once_and_does_not_return_it(client, monkeypatch):
    received = []

    def pair(ip, pairing_port, connection_port, code):
        received.append((ip, pairing_port, connection_port, code))
        return {"paired": True, "connected": True, "serial": f"{ip}:{connection_port}", "state": "device"}

    monkeypatch.setattr(device, "pair_wireless_device", pair)
    response = client.post(
        "/api/v1/devices/wireless-pair",
        json={"ip": "192.168.1.100", "pairing_port": 37123, "connection_port": 40567, "pairing_code": "123456"},
    )

    assert response.status_code == 200
    assert response.json() == {"paired": True, "connected": True, "serial": "192.168.1.100:40567", "state": "device"}
    assert received == [("192.168.1.100", 37123, 40567, "123456")]
    assert "123456" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "change",
    [
        {"ip": "127.0.0.1"},
        {"ip": "8.8.8.8"},
        {"ip": "example.com"},
        {"pairing_port": 0},
        {"pairing_code": "12345x"},
    ],
)
def test_pairing_route_rejects_invalid_input_without_echoing_code(client, monkeypatch, change):
    monkeypatch.setattr(device, "pair_wireless_device", lambda *_args: pytest.fail("must not run adb"))
    payload = {"ip": "192.168.1.100", "pairing_port": 37123, "connection_port": 40567, "pairing_code": "123456"}
    payload.update(change)
    response = client.post("/api/v1/devices/wireless-pair", json=payload)

    assert response.status_code == 422
    assert "123456" not in response.text


def test_same_port_is_accepted_for_auto_discovery(client, monkeypatch):
    received = []
    monkeypatch.setattr(device, "pair_wireless_device", lambda *args: received.append(args) or {"paired": True, "connected": False, "serial": None})
    response = client.post(
        "/api/v1/devices/wireless-pair",
        json={"ip": "192.168.1.100", "pairing_port": 37123, "connection_port": 37123, "pairing_code": "123456"},
    )
    assert response.status_code == 200
    assert response.json()["paired"] is True
    assert received == [("192.168.1.100", 37123, 37123, "123456")]


def test_pairing_uses_stdin_then_connects_and_verifies_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(device, "_adb_binary", lambda: "/usr/local/bin/adb")

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1] == "devices":
            return SimpleNamespace(returncode=0, stdout=b"List of devices attached\n192.168.1.100:40567 device model:Pixel\n")
        return SimpleNamespace(returncode=0, stdout=b"ok")

    monkeypatch.setattr(device.subprocess, "run", run)

    result = device.pair_wireless_device("192.168.1.100", 37123, 40567, "123456")

    assert result == {"paired": True, "connected": True, "serial": "192.168.1.100:40567", "state": "device"}
    assert calls[1][0] == ["/usr/local/bin/adb", "pair", "192.168.1.100:37123"]
    assert calls[1][1]["input"] == b"123456\n"
    assert calls[2][0] == ["/usr/local/bin/adb", "connect", "192.168.1.100:40567"]
    assert calls[3][0] == ["/usr/local/bin/adb", "devices", "-l"]
    assert all("123456" not in " ".join(arguments) for arguments, _ in calls)


def test_pairing_failure_and_missing_transport_have_safe_results(monkeypatch):
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")
    monkeypatch.setattr(
        device.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b"secret 123456"),
    )
    with pytest.raises(IntegrationError) as error:
        device.pair_wireless_device("192.168.1.100", 37123, 40567, "123456")
    assert error.value.code == "ADB_PAIR_FAILED"
    assert "123456" not in str(error.value)

    def run(arguments, **_kwargs):
        output = b"List of devices attached\n" if arguments[1] == "devices" else b"ok"
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(device.subprocess, "run", run)
    result = device.pair_wireless_device("192.168.1.100", 37123, 40567, "123456")
    assert result == {"paired": True, "connected": False, "serial": None}


def test_pairing_discovers_connection_port_from_same_ip(monkeypatch):
    calls = []
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1] == "mdns":
            output = ("List of discovered mdns services\n"
                      "adb-phone _adb-tls-pairing._tcp 192.168.1.100:37123\n"
                      "adb-phone _adb-tls-connect._tcp 192.168.1.100:40567\n"
                      "adb-other _adb-tls-connect._tcp 192.168.1.101:50000\n")
        elif arguments[1] == "devices":
            output = "List of devices attached\n192.168.1.100:40567 device\n"
        else:
            output = "ok"
        return SimpleNamespace(returncode=0, stdout=output.encode())

    monkeypatch.setattr(device.subprocess, "run", run)
    result = device.pair_wireless_device("192.168.1.100", 37123, 37123, "123456")
    assert result["serial"] == "192.168.1.100:40567"
    assert [call[0][1] for call in calls] == ["mdns", "pair", "mdns", "connect", "devices"]


def test_connection_port_entered_as_pairing_port_uses_discovered_pairing_service(monkeypatch):
    calls = []
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1] == "mdns":
            output = ("List of discovered mdns services\n"
                      "adb-phone _adb-tls-pairing._tcp 192.168.1.104:34511\n"
                      "adb-phone _adb-tls-connect._tcp 192.168.1.104:33949\n")
        elif arguments[1] == "devices":
            output = "List of devices attached\n192.168.1.104:33949 device\n"
        else:
            output = "ok"
        return SimpleNamespace(returncode=0, stdout=output.encode())

    monkeypatch.setattr(device.subprocess, "run", run)
    result = device.pair_wireless_device("192.168.1.104", 33949, None, "123456")
    assert result["serial"] == "192.168.1.104:33949"
    assert [args for args, _ in calls if args[1] == "pair"] == [["adb", "pair", "192.168.1.104:34511"]]
    assert [args for args, _ in calls if args[1] == "connect"] == [["adb", "connect", "192.168.1.104:33949"]]


def test_known_connection_port_without_pairing_service_explains_wrong_port(monkeypatch):
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")
    monkeypatch.setattr(
        device, "_mdns_services",
        lambda _adb: [("adb-phone", "_adb-tls-connect._tcp", "192.168.1.104", 33949)],
    )
    monkeypatch.setattr(device, "_pair_at", lambda *_args: pytest.fail("must not pair on connection port"))
    with pytest.raises(IntegrationError) as error:
        device.pair_wireless_device("192.168.1.104", 33949, None, "123456")
    assert error.value.code == "ADB_PAIR_PORT_IS_CONNECT"


def test_pairing_without_mdns_can_connect_later_without_pairing_code(client, monkeypatch):
    calls = []
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")
    monkeypatch.setattr(device.time, "sleep", lambda _seconds: None)

    def run(arguments, **kwargs):
        calls.append(arguments)
        output = "List of devices attached\n192.168.1.100:40567 device\n" if arguments[1] == "devices" else "ok"
        return SimpleNamespace(returncode=0, stdout=output.encode())

    monkeypatch.setattr(device.subprocess, "run", run)
    paired = client.post("/api/v1/devices/wireless-pair", json={
        "ip": "192.168.1.100", "pairing_port": 37123, "pairing_code": "123456",
    })
    assert paired.status_code == 200
    assert paired.json() == {"paired": True, "connected": False, "serial": None}
    connected = client.post("/api/v1/devices/wireless-connect", json={
        "ip": "192.168.1.100", "connection_port": 40567,
    })
    assert connected.status_code == 200
    assert connected.json() == {"serial": "192.168.1.100:40567", "state": "device"}
    assert [arguments[1] for arguments in calls].count("pair") == 1


def test_connection_accepts_mdns_transport_for_same_ip_and_port(monkeypatch):
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")
    alias = "adb-phone._adb-tls-connect._tcp"

    def command(arguments, **_kwargs):
        if arguments[1] == "devices":
            return f"List of devices attached\n{alias} device\n"
        if arguments[1] == "mdns":
            return "List of discovered mdns services\nadb-phone _adb-tls-connect._tcp 192.168.1.100:40567\n"
        return "connected"

    monkeypatch.setattr(device, "_run_command", command)
    assert device.connect_wireless_device("192.168.1.100", 40567) == {"serial": alias, "state": "device"}


def test_qr_session_waits_for_matching_service_then_pairs_once(monkeypatch):
    monkeypatch.setattr(device, "_QR_SESSION", None)
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")
    payloads = []
    monkeypatch.setattr(device, "_render_pairing_qr", lambda payload: payloads.append(payload) or b"\x89PNG\r\n\x1a\nimage")
    services = []
    monkeypatch.setattr(device, "_mdns_services", lambda _adb: services)
    paired = []
    monkeypatch.setattr(device, "_pair_at", lambda *args: paired.append(args))
    monkeypatch.setattr(device, "_after_pair", lambda *_args: {"paired": True, "connected": True, "serial": "192.168.1.100:40567", "state": "device"})

    started = device.start_wireless_qr()
    assert started["image"].startswith("data:image/png;base64,")
    assert "P:" not in str(started)
    assert device.poll_wireless_qr(started["session_id"]) == {"status": "waiting"}
    service = device._QR_SESSION["service"]
    secret = device._QR_SESSION["secret"]
    assert payloads == [f"WIFI:T:ADB;S:{service};P:{secret};;"]
    services.append((service, "_adb-tls-pairing._tcp", "192.168.1.100", 37123))
    result = device.poll_wireless_qr(started["session_id"])
    assert result == {"status": "connected", "ip": "192.168.1.100", "serial": "192.168.1.100:40567"}
    assert paired == [("adb", "192.168.1.100:37123", secret)]
    assert device._QR_SESSION["secret"] == ""
    assert device.poll_wireless_qr(started["session_id"]) == result
    assert len(paired) == 1


def test_qr_routes_return_transient_image_and_status(client, monkeypatch):
    monkeypatch.setattr(
        device, "start_wireless_qr",
        lambda: {"session_id": "test-session", "image": "data:image/png;base64,iVBORw0KGgo=", "expires_in": 120},
    )
    monkeypatch.setattr(device, "poll_wireless_qr", lambda token: {"status": "waiting", "session": token})
    started = client.post("/api/v1/devices/wireless-qr", json={})
    assert started.status_code == 200
    assert started.json()["session_id"] == "test-session"
    assert started.headers["Cache-Control"] == "no-store"
    polled = client.post("/api/v1/devices/wireless-qr/test-session/poll", json={})
    assert polled.status_code == 200
    assert polled.json() == {"status": "waiting", "session": "test-session"}


def test_qr_expiry_does_not_run_adb(monkeypatch):
    monkeypatch.setattr(device, "_QR_SESSION", None)
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")
    monkeypatch.setattr(device, "_render_pairing_qr", lambda _payload: b"\x89PNG\r\n\x1a\nimage")
    started = device.start_wireless_qr()
    device._QR_SESSION["expires_at"] = 0
    monkeypatch.setattr(device, "_mdns_services", lambda _adb: pytest.fail("expired QR must not inspect ADB"))
    assert device.poll_wireless_qr(started["session_id"]) == {"status": "expired"}
    with pytest.raises(IntegrationError) as error:
        device.poll_wireless_qr(started["session_id"])
    assert error.value.code == "QR_SESSION_NOT_FOUND"


def test_pairing_timeout_is_reported_without_the_code(monkeypatch):
    monkeypatch.setattr(device, "_adb_binary", lambda: "adb")

    def run(arguments, **_kwargs):
        raise subprocess.TimeoutExpired(arguments, 20)

    monkeypatch.setattr(device.subprocess, "run", run)
    with pytest.raises(IntegrationError) as error:
        device.pair_wireless_device("192.168.1.100", 37123, 40567, "123456")
    assert error.value.code == "ADB_PAIR_TIMEOUT"
    assert "123456" not in str(error.value)
