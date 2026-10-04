"""Spatial mobile chat gesture regression check (Playwright + Microsoft Edge)."""

from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


def main():
    html = (ROOT / 'static/index.html').read_text(encoding='utf-8')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 960}, has_touch=True, is_mobile=True)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request):
            if request.request.url == 'http://mobile.test/':
                request.fulfill(body=html, content_type='text/html; charset=utf-8')
            elif 'cdnjs' in request.request.url:
                request.fulfill(body='', content_type='text/css')
            else:
                request.fulfill(body='{"results":[]}', content_type='application/json')

        page.route('**/*', route)
        page.goto('http://mobile.test/')
        page.evaluate("""() => {
            collapseSidebar();
            aiMessages.innerHTML = [
                '<div class="msg-row ai"><div class="msg-bubble" style="width:270px">',
                '<strong id="replyText">First reply</strong><br><br><span id="replySecond">Second line</span>',
                '<p id="replyParagraph" style="margin:12px 0">Paragraph text</p>',
                '<span id="spaceFirst">One</span>    <span id="spaceSecond">Two</span>',
                '</div></div><div class="msg-row user"><div class="msg-quote-tag">Selected example</div>',
                '<div class="msg-bubble" style="width:220px">User question</div></div>'
            ].join('');
            aiInput.value = 'Keep my draft';
        }""")
        content = page.locator('#aiMessages').inner_html()

        def expanded():
            return page.locator('#aiPhoneExpandBtn').get_attribute('aria-expanded') == 'true'

        def point(area):
            target = {'padding': '.msg-row.ai .msg-bubble', 'blank line': '#replyText',
                      'line end': '#replyText', 'paragraph': '#replyParagraph',
                      'between words': '#spaceFirst'}.get(area, area)
            page.locator(target).scroll_into_view_if_needed()
            return page.evaluate("""area => {
                if (area.startsWith('#') || area.startsWith('.')) {
                    const container = document.querySelector(area).getBoundingClientRect();
                    return [container.right-4,container.top+4];
                }
                const bubble = document.querySelector('.msg-row.ai .msg-bubble').getBoundingClientRect();
                const text = document.getElementById('replyText').getBoundingClientRect();
                const lineHeight = parseFloat(getComputedStyle(document.querySelector('.msg-bubble')).lineHeight);
                if (area === 'padding') return [bubble.left+4,bubble.top+4];
                if (area === 'blank line') return [text.left+4,text.top+lineHeight+4];
                if (area === 'line end') return [bubble.right-5,text.top+5];
                if (area === 'paragraph') {
                    const p = document.getElementById('replyParagraph').getBoundingClientRect();
                    return [p.right-5,p.top+5];
                }
                if (area === 'between words') {
                    const first = document.getElementById('spaceFirst').getBoundingClientRect();
                    const second = document.getElementById('spaceSecond').getBoundingClientRect();
                    return [(first.right+second.left)/2,first.top+5];
                }
                const container = document.querySelector(area).getBoundingClientRect();
                return [container.right-4,container.top+4];
            }""", area)

        for area in ['padding', 'blank line', 'line end', 'paragraph', 'between words',
                     '#aiMessages', '.msg-row.user .msg-bubble', '.msg-quote-tag']:
            for expected in [True, False]:
                x, y = point(area)
                page.touchscreen.tap(x, y)
                assert expanded() != expected, f'{area}: single tap toggled'
                page.touchscreen.tap(x, y)
                page.wait_for_timeout(400)
                assert expanded() == expected, f'{area}: double tap did not toggle exactly once'
            print(f'PASS touch expand/restore: {area}')

        page.wait_for_timeout(550)
        page.mouse.dblclick(*point('padding'))
        assert expanded()
        page.mouse.dblclick(*point('paragraph'))
        assert not expanded()
        page.locator('#replyText').dblclick()
        assert not expanded(), 'Double click on formatted text toggled'
        page.locator('#aiInput').dblclick()
        assert not expanded(), 'Input toggled'
        page.locator('.msg-row.ai .msg-bubble').scroll_into_view_if_needed()
        page.evaluate("""() => {
            const el = document.querySelector('.msg-row.ai .msg-bubble');
            const rect = el.getBoundingClientRect();
            const x = rect.left+4, y = rect.top+4;
            const send = (type,dy) => el.dispatchEvent(new PointerEvent(type, {
                bubbles:true,pointerType:'touch',pointerId:1,clientX:x,clientY:y+dy
            }));
            send('pointerdown',0);send('pointermove',40);send('pointerup',0);
            send('pointerdown',0);send('pointerup',0);
        }""")
        assert not expanded(), 'Swipe followed by single tap toggled'
        assert page.locator('#aiInput').input_value() == 'Keep my draft'
        assert page.locator('#aiMessages').inner_html() == content
        for lines in [1, 80]:
            page.evaluate("""lines => {
                aiBlankPointer = aiLastBlankTap = null;
                aiMessages.innerHTML = '<div class="msg-row ai"><div class="msg-bubble">' + 'Old reply<br>'.repeat(40) + '</div></div>'
                    + '<div class="msg-row user"><div class="msg-bubble">Latest question</div></div>'
                    + '<div class="msg-row ai"><div class="msg-bubble" id="latestReply">' + 'Latest reply first line<br>'.repeat(lines) + '</div></div>'
                    + '<div class="msg-row ai"><div class="msg-bubble msg-loading">Thinking</div></div>';
                aiMessages.scrollTop = aiMessages.scrollHeight;
            }""", lines)
            # Start at the bottom of the history and expand with a real double tap.
            for _ in range(2):
                page.touchscreen.tap(*point('#aiMessages'))
            page.wait_for_timeout(400)
            assert expanded()
            offset = page.locator('#latestReply').evaluate("""el => {
                const messages = document.getElementById('aiMessages');
                return el.getBoundingClientRect().top - messages.getBoundingClientRect().top
                    - parseFloat(getComputedStyle(messages).paddingTop);
            }""")
            assert abs(offset) < 1, f'Latest reply did not align at its beginning: {offset}'
            assert page.locator('#aiInput').input_value() == 'Keep my draft'
            page.locator('#aiPhoneExpandBtn').click()
            assert not expanded()
            print(f'PASS latest reply starts at first line ({lines} lines), ignoring loading indicator')
        page.set_viewport_size({'width': 900, 'height': 960})
        page.mouse.dblclick(*point('.ai-hd'))
        assert not expanded(), 'Tablet gesture changed'
        assert not errors, errors
        browser.close()
        print('PASS mouse, text/input protection, swipe protection, preserved content and tablet layout')


if __name__ == '__main__':
    main()
