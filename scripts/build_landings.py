#!/usr/bin/env python3
"""Собирает лендинги для соцсетей из одного источника — tiktok.html.

Источник правды: tiktok.html в корне. Отсюда генерятся короткие адреса,
чтобы в шапке TikTok не висели хвосты с параметрами:

    tt/index.html      -> бот, метка ref_Yanprav
    lookup/index.html  -> канал @lookup_platform

Запуск после любой правки tiktok.html:  python3 scripts/build_landings.py
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "tiktok.html"

# куда собираем -> какой Telegram-адрес зашит по умолчанию (пусто = бот)
LANDINGS = {
    "tt": "",
    "lookup": "lookup_platform",
    "markets": "lookup_markets",
}

ANCHOR = 'var to  = (qs.get("to") || "")'


def build(html: str, default_to: str) -> str:
    if not default_to:
        return html
    patched = html.replace(ANCHOR, f'var to  = (qs.get("to") || "{default_to}")')
    if patched == html:
        sys.exit(f"build_landings: не найден якорь в tiktok.html:\n  {ANCHOR}")
    return patched.replace(
        '<meta property="og:title" content="VPN Bot в Telegram">',
        f'<meta property="og:title" content="@{default_to} в Telegram">',
    )


def main() -> None:
    html = SRC.read_text(encoding="utf-8")
    if not re.search(r"qs\.get\(\"to\"\)", html):
        sys.exit("build_landings: в tiktok.html нет поддержки ?to= — обнови источник")

    for folder, default_to in LANDINGS.items():
        out = ROOT / folder / "index.html"
        out.parent.mkdir(exist_ok=True)
        out.write_text(build(html, default_to), encoding="utf-8")
        print(f"{out.relative_to(ROOT)}  ->  {default_to or 'бот ' + 'telega_vpn_robot'}")


if __name__ == "__main__":
    main()
