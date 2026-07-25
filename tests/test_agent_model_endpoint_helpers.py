"""Tests for the module-local helpers backing /api/v1/agent/model (Task B4).

These are the small fail-soft helpers `daemon.py` wires into the GET/POST
handler: fetching the SKGateway model list, reading skos.models role names,
and resolving a role selection to its concrete model for display. All three
must never raise into the HTTP handler, only degrade gracefully.
"""

from __future__ import annotations

from unittest.mock import patch

from skchat.daemon import _fetch_gateway_models, _resolved_model_for, _skos_roles


class TestFetchGatewayModels:
    def test_returns_parsed_json_on_success(self) -> None:
        payload = b'{"data": [{"id": "ornith-tiny"}]}'

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return payload

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            result = _fetch_gateway_models()
        assert result == {"data": [{"id": "ornith-tiny"}]}

    def test_fails_soft_to_empty_dict_on_connection_error(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            assert _fetch_gateway_models() == {}

    def test_fails_soft_to_empty_dict_on_bad_json(self) -> None:
        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"not json"

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            assert _fetch_gateway_models() == {}

    def test_fails_soft_to_empty_dict_on_non_dict_json(self) -> None:
        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"[1, 2, 3]"

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            assert _fetch_gateway_models() == {}

    def test_derives_models_url_from_skchat_llm_url(self, monkeypatch) -> None:
        monkeypatch.setenv("SKCHAT_LLM_URL", "http://example:9999/v1/chat/completions")
        seen = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def _capture(url, timeout=None):
            seen["url"] = url
            return _FakeResp()

        with patch("urllib.request.urlopen", side_effect=_capture):
            _fetch_gateway_models()
        assert seen["url"] == "http://example:9999/v1/models"

    def test_falls_back_to_local_gateway_default_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("SKCHAT_LLM_URL", raising=False)
        seen = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def _capture(url, timeout=None):
            seen["url"] = url
            return _FakeResp()

        with patch("urllib.request.urlopen", side_effect=_capture):
            _fetch_gateway_models()
        assert seen["url"] == "http://localhost:18780/v1/models"


class TestSkosRoles:
    def test_empty_list_when_skos_not_importable(self) -> None:
        with patch.dict("sys.modules", {"skos.models": None, "skos": None}):
            assert _skos_roles() == []

    def test_returns_role_names_from_registry(self) -> None:
        fake_module = type(
            "FakeSkosModels", (), {"list_roles": staticmethod(lambda: {"sk-default": "x"})}
        )
        with patch.dict("sys.modules", {"skos.models": fake_module}):
            assert _skos_roles() == ["sk-default"]

    def test_empty_list_on_registry_error(self) -> None:
        def _boom():
            raise RuntimeError("registry unreadable")

        fake_module = type("FakeSkosModels", (), {"list_roles": staticmethod(_boom)})
        with patch.dict("sys.modules", {"skos.models": fake_module}):
            assert _skos_roles() == []


class TestResolvedModelFor:
    def test_concrete_model_selection_returns_as_is(self) -> None:
        assert _resolved_model_for("ornith-tiny", "model") == "ornith-tiny"

    def test_role_selection_resolves_via_skos(self) -> None:
        class _Backend:
            model = "ornith-tiny"

        fake_module = type(
            "FakeSkosModels", (), {"resolve": staticmethod(lambda role=None: _Backend())}
        )
        with patch.dict("sys.modules", {"skos.models": fake_module}):
            assert _resolved_model_for("sk-default", "role") == "ornith-tiny"

    def test_role_selection_falls_back_when_skos_unavailable(self) -> None:
        with patch.dict("sys.modules", {"skos.models": None, "skos": None}):
            assert _resolved_model_for("sk-default", "role") == "sk-default"

    def test_role_selection_falls_back_on_resolve_error(self) -> None:
        def _boom(role=None):
            raise RuntimeError("registry unreadable")

        fake_module = type("FakeSkosModels", (), {"resolve": staticmethod(_boom)})
        with patch.dict("sys.modules", {"skos.models": fake_module}):
            assert _resolved_model_for("sk-default", "role") == "sk-default"
