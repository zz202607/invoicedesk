from __future__ import annotations

from app.core.text import (
    TextLine,
    extract_lines,
    nearest_line,
    nearest_right,
    normalize,
    same_row,
    strip_label,
)


def line(text: str, x0: float, y0: float, x1: float, y1: float) -> TextLine:
    return TextLine(text=text, x0=x0, y0=y0, x1=x1, y1=y1)


def test_normalize_strips_terminal_control_characters() -> None:
    assert normalize("安全\x1b[2J文本") == "安全[2J文本"
    assert "\x1b" not in normalize("\x07示例\x00公司")
    assert normalize("a" + chr(0x202E) + "b" + chr(0x2066) + "c" + chr(0x85) + "d") == "abcd"


def test_same_row_uses_tolerance() -> None:
    assert same_row(line("a", 0, 100, 10, 110), line("b", 20, 103, 30, 113)) is True
    assert same_row(line("a", 0, 100, 10, 110), line("b", 20, 140, 30, 150)) is False


def test_nearest_right_picks_same_row_closest_value() -> None:
    anchor = line("发票号码:", 100, 100, 160, 110)
    close = line("12345678", 170, 100, 230, 110)
    far = line("87654321", 260, 100, 320, 110)
    below = line("99999999", 100, 140, 160, 150)
    assert nearest_right(anchor, [far, below, close]) is close


def test_nearest_right_rejects_far_or_wrong_row() -> None:
    anchor = line("发票号码:", 100, 100, 160, 110)
    below = line("12345678", 100, 140, 160, 150)
    beyond = line("12345678", 400, 100, 460, 110)
    assert nearest_right(anchor, [below]) is None
    assert nearest_right(anchor, [beyond], max_dx=50) is None


def test_nearest_line_finds_value_below() -> None:
    anchor = line("价税合计", 100, 100, 160, 110)
    below = line("¥100.00", 110, 130, 170, 140)
    assert nearest_line(anchor, [below]) is below


def test_strip_label_removes_prefix_only() -> None:
    assert strip_label("名称：示例甲公司", "名称") == "示例甲公司"
    assert strip_label("示例甲公司", "名称") == "示例甲公司"


def test_extract_lines_clips_long_lines() -> None:
    class FakePage:
        def get_text(self, kind: str):
            return {
                "blocks": [
                    {
                        "type": 0,
                        "lines": [
                            {"bbox": (0, 0, 10, 10), "spans": [{"text": "A" * 5000}]}
                        ],
                    }
                ]
            }

    lines = extract_lines(FakePage())
    assert len(lines) == 1
    assert len(lines[0].text) == 2000
