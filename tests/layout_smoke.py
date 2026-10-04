"""Browser checks for the integrated desktop, tablet and phone tutor layouts."""

from pathlib import Path
import json

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            for width, native in [(1440, False), (1024, False), (1280, True), (800, True), (390, True), (360, True)]:
                context = browser.new_context(viewport={"width": width, "height": 900}, has_touch=width <= 600)
                if native:
                    context.add_init_script("window.androidNativeRuntime = true")
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                def respond(route):
                    url = route.request.url
                    if url.endswith('/api/translate_subtitles'):
                        sentences = route.request.post_data_json['sentences']
                        body = json.dumps({'status':'success','translations':[{'id':item['id'],'translation':'这是一条准确对应的中文译文。'} for item in sentences]})
                    elif url.endswith('/api/settings'):
                        body = json.dumps({'configured':{},'options':{'translation_provider':'qwen'}})
                    else:
                        body = html if url == 'http://layout.test/' else '{}'
                    route.fulfill(body=body,content_type='text/html' if url == 'http://layout.test/' else 'application/json')
                page.route("**/*", respond)
                page.goto("http://layout.test/")
                page.evaluate("toast('<img onerror=bad>字幕连接失败','info')")
                notice = page.locator('.toast').last
                assert notice.bounding_box()['y'] < 100
                assert notice.evaluate("el => getComputedStyle(el).animationName") == 'none'
                assert notice.locator('img').count() == 0
                page.evaluate("document.getElementById('toastWrap').innerHTML=''")
                page.evaluate("collapseSidebar(); appendChatMessage('ai', 'A long answer. '.repeat(2000))")
                page.evaluate("nowPlaying={url:'https://example.com/audio.mp3'};cues=[{start:0,end:10,text:'This sentence stays in English.'}];document.getElementById('welcome').style.display='none';document.getElementById('subScroll').style.display='block';renderSubs()")
                save = page.locator('#sbtn-0')
                assert save.is_visible() and save.evaluate("el => getComputedStyle(el).opacity") == '1'
                assert save.bounding_box()['height'] >= 36
                assert save.inner_text().strip() == ''
                assert save.locator('.fa-bookmark').count() == 1
                assert save.evaluate("el => getComputedStyle(el).backgroundColor") == 'rgba(0, 0, 0, 0)'
                assert save.evaluate("el => getComputedStyle(el).borderTopWidth") == '0px'
                assert save.locator('.fa-regular.fa-bookmark').count() == 1
                save.click()
                assert save.get_attribute('aria-pressed') == 'true'
                assert save.inner_text().strip() == ''
                assert save.locator('.fa-solid.fa-bookmark').count() == 1
                assert save.evaluate("el => getComputedStyle(el).getPropertyValue('--purple-light').trim()")
                save.click()
                assert save.get_attribute('aria-pressed') == 'false'
                assert save.inner_text().strip() == ''
                assert save.locator('.fa-regular.fa-bookmark').count() == 1
                assert page.evaluate("document.getElementById('subScroll').scrollWidth <= document.getElementById('subScroll').clientWidth")
                assert page.locator('#subtitleTranslateBtn').is_enabled()
                page.locator('#subtitleTranslateBtn').click()
                page.wait_for_function("document.querySelector('.sub-translation') !== null")
                assert page.locator('.sub-translation').inner_text() == '这是一条准确对应的中文译文。'
                assert 'This' in page.locator('.sub-txt').inner_text()
                page.locator('#subtitleTranslateBtn').click()
                assert page.locator('.sub-translation').count() == 0
                assert page.evaluate("new Set([...document.querySelectorAll('[id]')].map(e => e.id)).size === document.querySelectorAll('[id]').length")
                assert page.evaluate("document.getElementById('aiMessages').scrollHeight > document.getElementById('aiMessages').clientHeight")
                if width > 600:
                    page.wait_for_function("document.getElementById('colLeft').getBoundingClientRect().width === 0")
                    assert not page.locator("#aiPhoneExpandBtn").is_visible()
                    assert not page.locator(".ai-badge").is_visible()
                    bounds = "() => ['colLeft','colMid','colRight'].map(id => {let r=document.getElementById(id).getBoundingClientRect();return [r.x,r.y,r.width,r.height]})"
                    before = page.evaluate(bounds)
                    page.locator("#aiDesktopExpandBtn").click()
                    assert page.locator("#aiDesktopExpandBtn").get_attribute("aria-expanded") == "true"
                    assert page.evaluate(bounds) == before
                    assert abs(page.locator("#aiPanelShell").bounding_box()["width"] - width * 2 / 3) < 1
                    page.locator("#aiDesktopExpandBtn").click()
                    assert page.evaluate(bounds) == before
                    page.locator("#aiDesktopExpandBtn").click()
                    page.keyboard.press("Escape")
                    assert page.locator("#aiDesktopExpandBtn").get_attribute("aria-expanded") == "false"
                else:
                    assert not page.locator("#aiDesktopExpandBtn").is_visible()
                    assert page.locator(".ai-badge").is_visible()
                    page.locator("#aiPhoneExpandBtn").click()
                    assert not page.locator("#colMid").is_visible()
                    assert page.locator("#aiPhoneExpandBtn").get_attribute("aria-expanded") == "true"
                    page.keyboard.press("Escape")
                    assert page.locator("#colMid").is_visible()
                    # Use real touch events on the message list's blank padding.
                    box = page.locator("#aiMessages").bounding_box()
                    x, y = box["x"] + 2, box["y"] + box["height"] / 2
                    assert page.evaluate("([x,y]) => document.elementFromPoint(x,y).id", [x, y]) == "aiMessages"
                    page.touchscreen.tap(x, y)
                    page.touchscreen.tap(x, y)
                    assert page.locator("#aiPhoneExpandBtn").get_attribute("aria-expanded") == "true"
                    page.locator("#aiPhoneExpandBtn").click()
                    # A drag must not count as a tap in the double-tap gesture.
                    page.evaluate("""() => {
                        let target=document.getElementById('aiMessages');
                        for (let [type,y] of [['pointerdown',100],['pointermove',150],['pointerup',150]])
                            target.dispatchEvent(new PointerEvent(type,{bubbles:true,pointerType:'touch',pointerId:1,clientX:5,clientY:y}));
                    }""")
                    assert page.locator("#aiPhoneExpandBtn").get_attribute("aria-expanded") == "false"
                    page.locator("#aiPhoneExpandBtn").click()
                    page.set_viewport_size({"width": 800, "height": 900})
                    page.wait_for_function("!document.body.classList.contains('ai-expanded')")
                    page.locator("#aiDesktopExpandBtn").click()
                    page.set_viewport_size({"width": width, "height": 900})
                    page.wait_for_function("!document.getElementById('colRight').classList.contains('ai-expanded')")
                print(f"Layout passed: width={width}, native={native}")
                context.close()
            assert not errors, errors
        finally:
            browser.close()


if __name__ == "__main__":
    main()
