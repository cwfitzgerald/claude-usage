from click.testing import CliRunner

from claude_usage.server import main


def test_server_launcher_exposes_only_dashboard_options() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "--open / --no-open" in result.output
    assert "--projects-dir" in result.output
    assert "--host" in result.output
    assert "--port" in result.output
    assert "report" not in result.output
    assert "--json" not in result.output
