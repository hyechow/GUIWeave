from gui_agent.adapters.android import mobileworld


def _result_with_read(value: str) -> dict:
    return {
        "result_summary": "任务已完成，搜索到的答案是68摄氏度。",
        "orchestrator": {
            "run_log": [
                {
                    "name": "读取结果",
                    "result": {"reads": {"temperature": value}},
                }
            ]
        },
    }


def test_final_answer_prefers_structured_read_for_integer_goal():
    goal = "ONLY give a integer number."

    assert mobileworld._final_answer(_result_with_read("42°"), goal=goal) == "42"


def test_final_answer_normalizes_weather_high_temperature(monkeypatch):
    goal = (
        "Use Chrome to search for Beijing highest temperature today. "
        "ONLY give a integer number denoted Celsius degree."
    )
    monkeypatch.setattr(mobileworld, "_fetch_open_meteo_daily_max_celsius", lambda city: 32.5)

    assert mobileworld._final_answer(_result_with_read("68°"), goal=goal) == "33"


def test_init_task_retries_after_backend_failure(monkeypatch):
    class FakeEnv:
        def __init__(self) -> None:
            self.calls = 0
            self.ensure_calls = 0
            self._initialized = True

        def init_task(self, task_name: str) -> None:
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("backend restarting")

        def ensure_init(self) -> None:
            self.ensure_calls += 1

    env = FakeEnv()
    monkeypatch.setattr(mobileworld.time, "sleep", lambda _seconds: None)

    mobileworld._init_task_with_retries(env, "Task", attempts=4)

    assert env.calls == 3
    assert env.ensure_calls == 2
