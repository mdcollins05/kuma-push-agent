"""Tests for the pooled Kuma session manager and transient-error classification."""
from unittest.mock import MagicMock, patch

import pytest

import app.kuma as kuma


# Pooled-session state is reset between tests by the autouse fixture in conftest.


def _fake_api():
    api = MagicMock()
    api.sio.eio.state = "disconnected"
    return api


CREDS = ("http://kuma:3001", "user", "pw")


class TestSessionReuse:
    def test_second_call_reuses_the_same_connection(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()) as open_:
            with kuma.kuma_session(*CREDS) as a:
                pass
            with kuma.kuma_session(*CREDS) as b:
                pass
        assert a is b
        assert open_.call_count == 1

    def test_session_stays_open_after_a_successful_block(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()):
            with kuma.kuma_session(*CREDS) as api:
                pass
        assert kuma._session is api
        assert kuma.session_status()["open"] is True

    def test_credential_change_recycles(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()) as open_:
            with patch.object(kuma, "_teardown_session") as teardown:
                with kuma.kuma_session(*CREDS) as first:
                    pass
                with kuma.kuma_session("http://kuma:3001", "user", "different-pw") as second:
                    pass
        assert first is not second
        assert open_.call_count == 2
        teardown.assert_called_once_with(first)

    def test_session_older_than_max_age_is_recycled(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()) as open_:
            with patch.object(kuma, "_teardown_session"):
                with kuma.kuma_session(*CREDS) as first:
                    pass
                # Backdate the open time past the recycle threshold
                kuma._session_opened_at -= kuma.MAX_SESSION_AGE + 1
                with kuma.kuma_session(*CREDS) as second:
                    pass
        assert first is not second
        assert open_.call_count == 2


class TestSessionRecycleOnError:
    def test_error_inside_block_closes_the_session_and_propagates(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()):
            with patch.object(kuma, "_teardown_session") as teardown:
                with pytest.raises(RuntimeError, match="boom"):
                    with kuma.kuma_session(*CREDS) as api:
                        raise RuntimeError("boom")
                teardown.assert_called_once_with(api)
        assert kuma._session is None

    def test_next_call_after_an_error_reconnects(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()) as open_:
            with patch.object(kuma, "_teardown_session"):
                with pytest.raises(RuntimeError):
                    with kuma.kuma_session(*CREDS) as first:
                        raise RuntimeError("boom")
                with kuma.kuma_session(*CREDS) as second:
                    pass
        assert first is not second
        assert open_.call_count == 2

    def test_login_failure_tears_down_the_client(self):
        """A client that connects but fails login must not be left dangling."""
        api = _fake_api()
        api.login.side_effect = RuntimeError("bad creds")
        with patch.object(kuma, "UptimeKumaApi", return_value=api):
            with patch.object(kuma, "_teardown_session") as teardown:
                with pytest.raises(RuntimeError, match="bad creds"):
                    kuma._open_session(*CREDS)
                teardown.assert_called_once_with(api)


class TestFreshSession:
    def test_fresh_does_not_pool(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()):
            with patch.object(kuma, "_teardown_session") as teardown:
                with kuma.kuma_session(*CREDS, fresh=True) as api:
                    pass
                teardown.assert_called_once_with(api)
        assert kuma._session is None

    def test_fresh_leaves_an_existing_pooled_session_alone(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()):
            with patch.object(kuma, "_teardown_session"):
                with kuma.kuma_session(*CREDS) as pooled:
                    pass
                with kuma.kuma_session(*CREDS, fresh=True):
                    pass
        assert kuma._session is pooled

    def test_test_connection_forces_a_fresh_connection(self):
        """Reusing a pooled session would report success without testing the
        credentials that were passed in."""
        with patch.object(kuma, "kuma_session") as session:
            kuma.test_connection(*CREDS)
        assert session.call_args.kwargs.get("fresh") is True


class TestCloseSession:
    def test_close_is_a_noop_when_nothing_is_open(self):
        with patch.object(kuma, "_teardown_session") as teardown:
            kuma.close_session()
        teardown.assert_not_called()

    def test_close_tears_down_and_clears_state(self):
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()):
            with patch.object(kuma, "_teardown_session") as teardown:
                with kuma.kuma_session(*CREDS) as api:
                    pass
                kuma.close_session()
                teardown.assert_called_once_with(api)
        assert kuma._session is None
        assert kuma.session_status() == {"open": False, "age_seconds": None}


class TestIsTransientError:
    @pytest.mark.parametrize("exc", [
        __import__("socketio").exceptions.TimeoutError("timed out"),
        __import__("socketio").exceptions.ConnectionError("unable to connect"),
        __import__("socketio").exceptions.DisconnectedError("gone"),
        __import__("engineio").exceptions.ConnectionError("nope"),
        TimeoutError("socket timeout"),
        ConnectionRefusedError("refused"),
        __import__("httpx").ConnectError("no route"),
        __import__("httpx").ReadTimeout("slow"),
    ])
    def test_transient(self, exc):
        assert kuma.is_transient_error(exc) is True

    @pytest.mark.parametrize("message", [
        "unable to connect",
        "Connection is not established",
        "Request timed out",
    ])
    def test_transient_uptime_kuma_messages(self, message):
        assert kuma.is_transient_error(kuma.UptimeKumaException(message)) is True

    @pytest.mark.parametrize("exc", [
        ValueError("bad payload"),
        KeyError("pushToken"),
        TypeError("wrong type"),
        kuma.UptimeKumaException("Monitor does not exist"),
    ])
    def test_not_transient(self, exc):
        """Real bugs must keep their stack traces."""
        assert kuma.is_transient_error(exc) is False


class TestSuppressedErrorsInvalidate:
    """A caught error inside a pooled block must not leave a dead session behind.

    kuma_session() only recycles on exceptions that escape its yield, so call
    sites that catch and suppress have to invalidate explicitly — otherwise the
    next borrower inherits a connection that is already gone.
    """

    def test_transient_caught_error_drops_the_session(self):
        import socketio
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()):
            with patch.object(kuma, "_teardown_session"):
                with kuma.kuma_session(*CREDS):
                    dropped = kuma.invalidate_session_if_transient(
                        socketio.exceptions.DisconnectedError("gone")
                    )
        assert dropped is True
        assert kuma._session is None

    def test_non_transient_caught_error_keeps_the_session(self):
        """A tag that already exists is not a connection problem — stay connected."""
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()):
            with patch.object(kuma, "_teardown_session"):
                with kuma.kuma_session(*CREDS) as api:
                    dropped = kuma.invalidate_session_if_transient(ValueError("tag exists"))
        assert dropped is False
        assert kuma._session is api

    def test_apply_tags_drops_session_and_stops_on_dropped_connection(self):
        """get_push_token_and_apply_tags swallows tag errors, so it must invalidate
        itself — and stop, rather than work through a connection that is gone."""
        import socketio
        api = _fake_api()
        api.get_monitor.return_value = {"pushToken": "tok"}
        api.add_monitor_tag.side_effect = socketio.exceptions.DisconnectedError("gone")

        with patch.object(kuma, "_open_session", side_effect=lambda *a: api):
            with patch.object(kuma, "_teardown_session"):
                token = kuma.get_push_token_and_apply_tags(7, [1, 2, 3], *CREDS)

        assert token == "tok"
        assert api.add_monitor_tag.call_count == 1, "should stop after the connection drops"
        assert kuma._session is None

    def test_apply_tags_keeps_going_on_a_non_transient_tag_error(self):
        """One bad tag must not abandon the rest — existing behaviour."""
        api = _fake_api()
        api.get_monitor.return_value = {"pushToken": "tok"}
        api.add_monitor_tag.side_effect = ValueError("tag exists")

        with patch.object(kuma, "_open_session", side_effect=lambda *a: api):
            with patch.object(kuma, "_teardown_session"):
                token = kuma.get_push_token_and_apply_tags(7, [1, 2, 3], *CREDS)

        assert token == "tok"
        assert api.add_monitor_tag.call_count == 3
        assert kuma._session is api


class TestShutdownGate:
    def test_shutdown_pool_closes_and_blocks_new_borrows(self):
        """scheduler.shutdown(wait=False) does not wait, so a job still in flight
        must not be able to reopen the pool after teardown."""
        with patch.object(kuma, "_open_session", side_effect=lambda *a: _fake_api()):
            with patch.object(kuma, "_teardown_session") as teardown:
                with kuma.kuma_session(*CREDS) as api:
                    pass
                kuma.shutdown_pool()
                teardown.assert_called_once_with(api)

                with pytest.raises(RuntimeError, match="shutting down"):
                    with kuma.kuma_session(*CREDS):
                        pass
        assert kuma._session is None

    def test_shutdown_pool_is_safe_with_no_session_open(self):
        with patch.object(kuma, "_teardown_session") as teardown:
            kuma.shutdown_pool()
        teardown.assert_not_called()
        assert kuma._shutting_down is True


class TestTeardownSession:
    """The real teardown path, unmocked.

    Every other test in this file mocks _teardown_session, so without these the
    function introduced by this PR — and the leak fix it wraps — has no coverage
    at all. It is called from three paths the old inline finally never was: age
    recycle, error recycle, and shutdown.
    """

    @staticmethod
    def _api(*, state="disconnected", shutdown_raises=None):
        api = MagicMock()
        api.sio.eio.state = state
        api.sio.eio.ws = MagicMock()
        api.sio.eio.http = MagicMock()
        if shutdown_raises is not None:
            api.sio.shutdown.side_effect = shutdown_raises
        return api

    def test_shuts_down_and_closes_both_transports(self):
        api = self._api()
        kuma._teardown_session(api)
        api.sio.shutdown.assert_called_once_with()
        api.sio.eio.ws.close.assert_called_once_with()
        api.sio.eio.http.close.assert_called_once_with()

    def test_transports_still_closed_when_shutdown_raises(self):
        """The whole point of the force-close: shutdown() failing must not strand
        a live websocket, whose read-loop thread keeps the process alive."""
        api = self._api(shutdown_raises=RuntimeError("already gone"))
        kuma._teardown_session(api)  # must not raise
        api.sio.eio.ws.close.assert_called_once_with()
        api.sio.eio.http.close.assert_called_once_with()

    def test_warns_when_still_connected_after_shutdown(self, caplog):
        api = self._api(state="connected")
        with caplog.at_level("WARNING"):
            kuma._teardown_session(api)
        assert "force-closing transport" in caplog.text
        api.sio.eio.ws.close.assert_called_once_with()

    def test_no_warning_on_a_clean_shutdown(self, caplog):
        api = self._api()
        with caplog.at_level("WARNING"):
            kuma._teardown_session(api)
        assert caplog.text == ""

    def test_survives_a_transport_that_fails_to_close(self):
        """One resource refusing to close must not skip the other."""
        api = self._api()
        api.sio.eio.ws.close.side_effect = OSError("bad fd")
        kuma._teardown_session(api)  # must not raise
        api.sio.eio.http.close.assert_called_once_with()

    def test_handles_a_client_with_no_transports(self):
        """A client that failed mid-handshake has no ws/http yet."""
        api = MagicMock()
        api.sio.eio.state = "disconnected"
        api.sio.eio.ws = None
        api.sio.eio.http = None
        kuma._teardown_session(api)  # must not raise
        api.sio.shutdown.assert_called_once_with()
