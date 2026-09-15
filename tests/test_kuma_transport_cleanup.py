"""Tests for the transport-teardown helpers in app.kuma.

These are the v0.3.1 leak fixes (#35, d104850) and they had no coverage. Both
exist because the socketio/engineio state machines gate their own cleanup on
connection state, so a client can be abandoned while its websocket read-loop
thread is still running — which keeps the process alive and buffers Kuma's
heartbeat broadcasts forever.
"""
from unittest.mock import MagicMock, patch

import pytest

from app import kuma


def _sio(*, ws=True, http=True):
    sio = MagicMock()
    sio.eio.ws = MagicMock() if ws else None
    sio.eio.http = MagicMock() if http else None
    return sio


class TestCloseTransport:
    def test_closes_both_resources(self):
        sio = _sio()
        kuma._close_transport(sio)
        sio.eio.ws.close.assert_called_once_with()
        sio.eio.http.close.assert_called_once_with()

    @pytest.mark.parametrize("ws,http", [(True, False), (False, True), (False, False)])
    def test_skips_resources_that_were_never_created(self, ws, http):
        """A handshake that failed early leaves one or both unset."""
        sio = _sio(ws=ws, http=http)
        kuma._close_transport(sio)  # must not raise
        if ws:
            sio.eio.ws.close.assert_called_once_with()
        if http:
            sio.eio.http.close.assert_called_once_with()

    def test_one_failing_close_does_not_skip_the_other(self):
        """The websocket is the one holding the thread — it must still be closed
        even if the polling session blows up first, and vice versa."""
        sio = _sio()
        sio.eio.ws.close.side_effect = OSError("bad fd")
        kuma._close_transport(sio)  # must not raise
        sio.eio.http.close.assert_called_once_with()

    def test_handles_a_client_with_no_eio_at_all(self):
        sio = MagicMock()
        sio.eio = None
        kuma._close_transport(sio)  # must not raise

    def test_logs_a_warning_when_a_close_fails(self, caplog):
        sio = _sio()
        sio.eio.http.close.side_effect = RuntimeError("nope")
        with caplog.at_level("WARNING"):
            kuma._close_transport(sio)
        assert "transport close failed" in caplog.text

    def test_silent_on_a_clean_close(self, caplog):
        with caplog.at_level("WARNING"):
            kuma._close_transport(_sio())
        assert caplog.text == ""


class TestConnectWithCleanup:
    """UptimeKumaApi.__init__ calls connect() before the caller holds a reference,
    so a handshake that fails partway strands a client no caller-side teardown can
    reach. The wrapper makes connect() failures clean up after themselves."""

    def test_successful_connect_leaves_the_client_alone(self):
        api = MagicMock()
        with patch.object(kuma, "_orig_connect") as orig:
            kuma._connect_with_cleanup(api)
        orig.assert_called_once_with(api)
        api.sio.shutdown.assert_not_called()

    def test_failed_connect_shuts_down_closes_transports_and_reraises(self):
        api = MagicMock()
        api.sio.eio.ws = MagicMock()
        api.sio.eio.http = MagicMock()
        with patch.object(kuma, "_orig_connect", side_effect=RuntimeError("unable to connect")):
            with pytest.raises(RuntimeError, match="unable to connect"):
                kuma._connect_with_cleanup(api)
        api.sio.shutdown.assert_called_once_with()
        api.sio.eio.ws.close.assert_called_once_with()
        api.sio.eio.http.close.assert_called_once_with()

    def test_transports_closed_even_when_shutdown_also_fails(self):
        """Both teardown routes failing must not stop the force-close — this is the
        exact path that leaked ~0.5 threads an hour in production."""
        api = MagicMock()
        api.sio.eio.ws = MagicMock()
        api.sio.eio.http = MagicMock()
        api.sio.shutdown.side_effect = RuntimeError("already gone")
        with patch.object(kuma, "_orig_connect", side_effect=RuntimeError("unable to connect")):
            with pytest.raises(RuntimeError, match="unable to connect"):
                kuma._connect_with_cleanup(api)
        api.sio.eio.ws.close.assert_called_once_with()
        api.sio.eio.http.close.assert_called_once_with()

    def test_the_wrapper_is_actually_installed(self):
        """A regression guard: the module patches UptimeKumaApi.connect at import."""
        from uptime_kuma_api import UptimeKumaApi
        assert UptimeKumaApi.connect is kuma._connect_with_cleanup
