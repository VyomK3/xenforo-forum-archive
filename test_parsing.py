"""Quick sanity test of the parsing functions against representative
XenForo 2.x markup (based on the standard 'default' style structure)."""

from bs4 import BeautifulSoup
from scrape_forum import get_child_forums, parse_thread_list, get_pagination_last_page

NODE_LIST_HTML = """
<html><body>
<div class="node-list">
  <div class="node node--forum" data-node-id="12">
    <div class="node-title"><a href="/community/mobile-phones-tablets.12/">Mobile Phones &amp; Tablets</a></div>
    <div class="node-description">Topics related to mobile phones, tablets</div>
  </div>
  <div class="node node--forum" data-node-id="13">
    <a class="node-title" href="/community/buying-advice.13/">Buying Advice</a>
    <div class="node-description">Sub-section for buying advice</div>
  </div>
</div>
</body></html>
"""

THREAD_LIST_HTML = """
<html><body>
<div class="structItemContainer">
  <div class="structItem structItem--thread is-sticky">
    <div class="structItem-title">
      <a class="labelLink" href="/community/threads/sticky-rules.100001/"><span class="label label--orange">Sticky</span></a>
      <a href="/community/threads/read-this-first.100001/">Read this first</a>
    </div>
    <div class="structItem-minor">
      <ul class="structItem-parts">
        <li><a href="/community/members/admin.1/">AdminUser</a></li>
        <li><time datetime="2015-01-01T10:00:00-0500" data-time="1420131600" title="Jan 1, 2015 at 10:00 AM">Jan 1, 2015</time></li>
      </ul>
    </div>
    <div class="structItem-cell structItem-cell--meta">
      <dl class="pairs pairs--justified"><dt>Replies</dt><dd>426</dd></dl>
      <dl class="pairs pairs--justified"><dt>Views</dt><dd>12K</dd></dl>
    </div>
    <div class="structItem-cell structItem-cell--latest">
      <a href="/community/members/lastuser.2/">LastUser</a>
      <time datetime="2024-01-01T10:00:00-0500" data-time="1704121200">Jan 1, 2024</time>
    </div>
  </div>
  <div class="structItem structItem--thread">
    <div class="structItem-title">
      <a href="/community/threads/normal-thread.100002/">A normal thread</a>
    </div>
    <div class="structItem-minor">
      <ul class="structItem-parts">
        <li><a href="/community/members/someone.3/">SomeUser</a></li>
        <li><time datetime="2020-05-01T10:00:00-0500" data-time="1588338000">May 1, 2020</time></li>
      </ul>
    </div>
    <div class="structItem-cell structItem-cell--meta">
      <dl class="pairs pairs--justified"><dt>Replies</dt><dd>5</dd></dl>
      <dl class="pairs pairs--justified"><dt>Views</dt><dd>340</dd></dl>
    </div>
    <div class="structItem-cell structItem-cell--latest">
      <a href="/community/members/someone2.4/">SomeUser2</a>
      <time datetime="2020-05-02T10:00:00-0500" data-time="1588424400">May 2, 2020</time>
    </div>
  </div>
</div>
<div class="pageNav-main">
  <ul>
    <li class="pageNav-page"><a href="/community/forums/mobile.12/page-1">1</a></li>
    <li class="pageNav-page"><a href="/community/forums/mobile.12/page-2">2</a></li>
    <li class="pageNav-page"><a href="/community/forums/mobile.12/page-5">5</a></li>
  </ul>
</div>
</body></html>
"""

def test_node_list():
    soup = BeautifulSoup(NODE_LIST_HTML, "html.parser")
    children = get_child_forums(soup, "xenforo_based_forum_URL")
    assert len(children) == 2, children
    assert children[0]["name"] == "Mobile Phones & Tablets"
    assert children[0]["url"].endswith("/mobile-phones-tablets.12/")
    assert children[1]["name"] == "Buying Advice"
    print("test_node_list passed:", children)

def test_thread_list():
    soup = BeautifulSoup(THREAD_LIST_HTML, "html.parser")
    threads = parse_thread_list(soup, "xenforo_based_forum_Section_URL")
    assert len(threads) == 2
    t1, t2 = threads
    assert t1["title"] == "Read this first"
    assert t1["poster"] == "AdminUser"
    assert t1["replies"] == 426
    assert t1["views"] == 12000
    assert t1["is_sticky"] == 1
    assert t1["last_post_author"] == "LastUser"
    assert t1["thread_id"] == "100001"
    assert t2["title"] == "A normal thread"
    assert t2["replies"] == 5
    assert t2["views"] == 340
    assert t2["is_sticky"] == 0
    print("test_thread_list passed:", threads)

def test_pagination():
    soup = BeautifulSoup(THREAD_LIST_HTML, "html.parser")
    last = get_pagination_last_page(soup)
    assert last == 5, last
    print("test_pagination passed:", last)

if __name__ == "__main__":
    test_node_list()
    test_thread_list()
    test_pagination()
    print("\nALL TESTS PASSED")
