from scripts.macos_autostart import launch_agent


def test_start_launch_agent_runs_on_weekends_and_leaves_server_alive():
    agent = launch_agent("/tmp/horse app", "com.example.horse", action="start")

    assert agent["StartCalendarInterval"] == [
        {"Weekday": 6, "Hour": 9, "Minute": 40},
        {"Weekday": 0, "Hour": 9, "Minute": 40},
    ]
    assert agent["AbandonProcessGroup"] is True
    assert agent["ProgramArguments"] == ["/tmp/horse app/scripts/open_keiba.sh"]


def test_stop_launch_agent_runs_weekends_at_1630_after_final_pipeline():
    agent = launch_agent("/tmp/horse app", "com.example.horse-stop", action="stop")

    assert agent["StartCalendarInterval"] == [
        {"Weekday": 6, "Hour": 16, "Minute": 30},
        {"Weekday": 0, "Hour": 16, "Minute": 30},
    ]
    assert agent["AbandonProcessGroup"] is False
    assert agent["ProgramArguments"][-1] == "/tmp/horse app/scripts/finalize_keiba.py"


def test_final_pipeline_timeout_does_not_skip_safe_shutdown(tmp_path, monkeypatch):
    from subprocess import TimeoutExpired

    from scripts import finalize_keiba

    calls = []
    monkeypatch.setattr(finalize_keiba, "LOG_PATH", tmp_path / "finalize.log")

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise TimeoutExpired(command, kwargs["timeout"])

        class Result:
            returncode = 0

        return Result()

    assert finalize_keiba.finalize(tmp_path, runner=runner) == 124
    assert calls[0][0][-1] == str(tmp_path / "apps/cli/daily_pipeline.py")
    assert calls[0][1]["timeout"] == 900
    assert calls[1][0] == [str(tmp_path / "scripts/close_keiba.sh")]
    assert "continuing shutdown" in (tmp_path / "finalize.log").read_text()
