"""System Monitoring scopes `docker service ls` to AEGIS's own swarm stack."""

from aegis.api.routes.system_status import _parse_service_lines, _stack_filter_args


def test_stack_filter_args_scopes_to_namespace_label():
    assert _stack_filter_args("aegis") == [
        "--filter",
        "label=com.docker.stack.namespace=aegis",
    ]


def test_stack_filter_args_blank_shows_all():
    # Empty stack = escape hatch: no filter, list every service.
    assert _stack_filter_args("") == []


def test_parse_service_lines_derives_stack_from_name():
    rows = _parse_service_lines("aegis_core 1/1 img:latest\naegis_worker 2/2 img:latest\n")
    assert [r["name"] for r in rows] == ["aegis_core", "aegis_worker"]
    assert {r["stack"] for r in rows} == {"aegis"}
