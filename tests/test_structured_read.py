from gui_agent.core.orchestrator.structured_read import _normalize_phone_read_value


def test_phone_read_prefers_tel_uri_digits():
    text_source = """
    +1 (555)
    123-4567
    tel:+15551234567
    """

    assert _normalize_phone_read_value(
        "电话号码",
        "+1 (555) 123-4567",
        "读取 PDF 正文中的电话号码",
        text_source,
    ) == "15551234567"


def test_phone_read_normalizes_formatted_value_without_tel_uri():
    assert _normalize_phone_read_value(
        "phone_number",
        "+1 (555) 123-4567",
    ) == "15551234567"


def test_non_phone_read_is_unchanged():
    assert _normalize_phone_read_value("document_title", "Kevin_CV.pdf") == "Kevin_CV.pdf"
