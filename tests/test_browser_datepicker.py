from types import SimpleNamespace

from gui_agent.adapters.browser.executor import BrowserExecutor


class _Client:
    def __init__(self, result):
        self.result = result
        self.script = ""

    def eval_js(self, script):
        self.script = script
        return self.result


def test_jquery_datepicker_notifies_direct_binding_without_bubbling_submit():
    client = _Client({"value": "01/1/2023", "event": "change"})

    assert BrowserExecutor(SimpleNamespace(client=client))._type_intercept(
        client, "01/01/2023"
    )
    assert "!window.jQuery" in client.script
    assert "new Event('change', {bubbles: false})" in client.script
    assert "trigger('change')" not in client.script


def test_datepicker_requires_value_readback_before_claiming_success():
    client = _Client(True)

    assert not BrowserExecutor(SimpleNamespace(client=client))._type_intercept(
        client, "01/01/2023"
    )
