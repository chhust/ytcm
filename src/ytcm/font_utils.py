import logging
import warnings

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

CJK_FONT_CANDIDATES = [
    "Arial Unicode MS",
    "Noto Sans CJK", "Noto Sans CJK KR", "Noto Sans CJK JP", "Noto Sans CJK SC", "Noto Sans CJK TC",
    "Noto Sans KR", "Noto Sans JP", "Noto Sans SC", "Noto Sans TC",
    "Malgun Gothic", "Apple SD Gothic Neo", "AppleGothic", "NanumGothic",
    "Meiryo", "Yu Gothic", "Hiragino Sans", "Hiragino Kaku Gothic Pro",
    "Microsoft YaHei", "SimHei", "PingFang SC", "PingFang HK", "PingFang TC",
    "Heiti TC", "Songti SC", "WenQuanYi Zen Hei", "Source Han Sans",
]

EMOJI_FONT_CANDIDATES = [
    "Noto Emoji", "Noto Sans Symbols 2", "Noto Sans Symbols",
    "Symbola", "OpenMoji", "Segoe UI Emoji", "Segoe UI Symbol",
    "Twemoji Mozilla", "JoyPixels", "EmojiOne Color", "Quivira",
]

_font_paths_by_name = None
_configured = False
_wordcloud_font_path = None


def _installed_fonts():
    global _font_paths_by_name
    if _font_paths_by_name is None:
        _font_paths_by_name = {}
        for f in fm.fontManager.ttflist:
            _font_paths_by_name.setdefault(f.name, f.fname)
    return _font_paths_by_name


def configure_cjk_fonts():
    global _configured, _wordcloud_font_path
    if _configured:
        return _wordcloud_font_path

    warnings.filterwarnings("ignore", message=r"Glyph \d+ .* missing from font",
                            category=UserWarning)

    installed = _installed_fonts()
    available = [name for name in CJK_FONT_CANDIDATES if name in installed]
    emoji = [name for name in EMOJI_FONT_CANDIDATES if name in installed]

    if available or emoji:
        plt.rcParams["font.family"] = available + emoji + ["sans-serif"]
        plt.rcParams["axes.unicode_minus"] = False

    if available:
        _wordcloud_font_path = installed[available[0]]
    else:
        logger.warning("No Korean/Japanese/Chinese-capable font found on this system; "
                        "CJK text may render as missing-glyph boxes.")

    _configured = True
    return _wordcloud_font_path


def get_wordcloud_font_path():
    return configure_cjk_fonts()
