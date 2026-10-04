"""Export Android brand artwork from the actual Web welcome icon.

Requires fonttools and Playwright (Edge). No screenshot supplied by a user is used.
The font's original outline is also used for adaptive and notification icons.
"""
import re
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / 'android/app/src/main/res'
FONT = ROOT / 'android/app/src/main/assets/webfonts/fa-solid-900.woff2'


def write(relative, value):
    path = RES / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + '\n', encoding='utf-8')


def main():
    html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
    # Read the component's actual CSS, rather than approximating its appearance.
    css = '\n'.join(re.search(r'\.' + name + r'\s*\{[^}]*\}', html).group()
                    for name in ('splash-icon-wrapper', 'splash-icon'))
    font = TTFont(FONT)
    glyphs = font.getGlyphSet()
    glyph = font.getBestCmap()[0xf58f]  # fa-headphones-simple
    outline = SVGPathPen(glyphs)
    # 42px glyph in the 96px Web tile, enlarged to Android's 108dp viewport.
    scale = 42 / font['head'].unitsPerEm * 108 / 96
    glyphs[glyph].draw(TransformPen(outline, (scale, 0, 0, -scale,
                                           27 * 108 / 96, 63.75 * 108 / 96)))
    path_data = outline.getCommands()
    write('drawable/ic_brand_foreground.xml', f'''<vector xmlns:android="http://schemas.android.com/apk/res/android" xmlns:aapt="http://schemas.android.com/aapt" android:width="108dp" android:height="108dp" android:viewportWidth="108" android:viewportHeight="108">
    <path android:pathData="{path_data}">
        <aapt:attr name="android:fillColor"><gradient android:startX="30.375" android:startY="30.375" android:endX="77.625" android:endY="77.625" android:type="linear"><item android:offset="0" android:color="#E9D5FF"/><item android:offset="0.5" android:color="#C084FC"/><item android:offset="1" android:color="#A855F7"/></gradient></aapt:attr>
    </path>
</vector>''')
    write('drawable/ic_brand_monochrome.xml', f'''<vector xmlns:android="http://schemas.android.com/apk/res/android" android:width="108dp" android:height="108dp" android:viewportWidth="108" android:viewportHeight="108">
    <path android:fillColor="#FFFFFFFF" android:pathData="{path_data}"/>
</vector>''')
    write('drawable/ic_brand_background.xml', '''<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">
    <gradient android:angle="315" android:startColor="#1D1330" android:endColor="#0F0C1C"/>
</shape>''')
    for folder, mono in [('mipmap-anydpi-v26', ''), ('mipmap-anydpi-v33', '\n    <monochrome android:drawable="@drawable/ic_brand_monochrome"/>')]:
        write(folder + '/ic_launcher_brand.xml', f'''<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_brand_background"/>
    <foreground android:drawable="@drawable/ic_brand_foreground"/>{mono}
</adaptive-icon>''')
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        def serve(route):
            if route.request.url.endswith('/font.woff2'):
                route.fulfill(body=FONT.read_bytes(), content_type='font/woff2')
            else:
                route.fulfill(body='')
        for folder, size in [('drawable-nodpi', 576), ('mipmap-mdpi', 48),
                             ('mipmap-hdpi', 72), ('mipmap-xhdpi', 96),
                             ('mipmap-xxhdpi', 144), ('mipmap-xxxhdpi', 192)]:
            context = browser.new_context(viewport={'width': 96, 'height': 96}, device_scale_factor=size / 96)
            page = context.new_page()
            page.route('**/*', serve)
            page.goto('http://brand.test/')
            page.set_content('''<style>@font-face{font-family:Brand;src:url('/font.woff2');font-weight:900}
                *{box-sizing:border-box}html,body{margin:0;background:transparent}
                ''' + css + '''
                .splash-icon-wrapper{margin:0;transition:none;box-shadow:inset 0 1px 1px rgba(255,255,255,.15)}
                .splash-icon{font-family:Brand;font-weight:900;font-style:normal;line-height:1}
                .splash-icon::before{content:'\\f58f'}
                </style><div class="splash-icon-wrapper"><i class="splash-icon"></i></div>''')
            page.evaluate('document.fonts.ready')
            name = 'brand_splash.png' if folder == 'drawable-nodpi' else 'ic_launcher_brand.png'
            output = RES / folder / name
            output.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output), omit_background=True)
            context.close()
        browser.close()
    print('Exported the Web icon and original font outline to Android resources.')


if __name__ == '__main__':
    main()
