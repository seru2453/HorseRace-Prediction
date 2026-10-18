from scripts.macos_autostart import launch_agent


def test_start_launch_agent_runs_on_saturday_and_leaves_server_alive():
    agent = launch_agent("/tmp/horse app", "com.example.horse", action="start")

    assert agent["StartCalendarInterval"] == {"Weekday": 6, "Hour": 9, "Minute": 40}
    assert agent["AbandonProcessGroup"] is True
    assert agent["ProgramArguments"] == ["/tmp/horse app/scripts/open_keiba.sh"]


def test_stop_launch_agent_runs_on_saturday_at_1630():
    agent = launch_agent("/tmp/horse app", "com.example.horse-stop", action="stop")

    assert agent["StartCalendarInterval"] == {"Weekday": 6, "Hour": 16, "Minute": 30}
    assert agent["AbandonProcessGroup"] is False
    assert agent["ProgramArguments"] == ["/tmp/horse app/scripts/close_keiba.sh"]
